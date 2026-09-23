
import os
import sys
import json
import time
import logging
import tempfile
import boto3

import db
from xtmt_client import upload_evidence_via_xtmt, XtmtError, XtmtCaseNameError
from ensure_uploadxt_login import ensure_login

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger()

RESOURCE_REGION = os.environ.get("AWS_RESOURCE_REGION", "us-east-2")
SQS_QUEUE_URL = os.environ.get(
    "SYNC_QUEUE_URL",
    "https://sqs.us-east-2.amazonaws.com/770410989764/sync-to-axon-jobs",
)

POLL_WAIT_TIME_SECONDS = 20
MAX_MESSAGES_PER_POLL = 1
VISIBILITY_TIMEOUT_SECONDS = 1800  # 30 min -- large files take longer

# Re-check Upload XT's login session every N processed jobs, in case it
# expires mid-run.
LOGIN_CHECK_EVERY_N_JOBS = 5

sqs = boto3.client("sqs", region_name=RESOURCE_REGION)
s3 = boto3.client("s3", region_name=RESOURCE_REGION)


def _download_evidence(bucket: str, key: str) -> str:
    local_dir = os.path.join(tempfile.gettempdir(), "axon-xtmt-staging")
    os.makedirs(local_dir, exist_ok=True)
    local_path = os.path.join(local_dir, os.path.basename(key))
    logger.info(f"Downloading s3://{bucket}/{key} -> {local_path}")
    s3.download_file(bucket, key, local_path)
    return local_path


def _process_job(message_body: dict):
    job_id = message_body["jobId"]
    case_number = message_body.get("caseNumber", "")
    evidence_list = message_body["evidence"]

    if not evidence_list:
        logger.info(f"Job {job_id}: no evidence items found, nothing to do.")
        return

    logger.info(f"=== Job {job_id}: processing {len(evidence_list)} evidence item(s) via XTMT ===")
    db.update_status(job_id, "IN_PROGRESS")

    previous_hashes = db.get_previous_hash_log(case_number) if case_number else {}

    for item in evidence_list:
        evidence_id = item["evidenceId"]
        local_path = None
        try:
            # Duplicate protection: skip if this exact evidence item was
            # already synced successfully in a prior job for this case.
            local_path = _download_evidence(item["s3Bucket"], item["s3Key"])
            from xtmt_client import file_sha256
            file_hash = file_sha256(local_path)

            if previous_hashes.get(evidence_id) == file_hash:
                logger.info(f"Skipping already-synced evidence {evidence_id} (hash match)")
                db.record_evidence_result(job_id, evidence_id, success=True, file_hash=file_hash)
                continue

            result = upload_evidence_via_xtmt(
                case_number=case_number,
                local_file_path=local_path,
                operation_name=f"dems-sync-{job_id[:8]}",
                description=f"DEMS automated sync -- job {job_id}",
            )

            if result["success"]:
                logger.info(f"Evidence {evidence_id} uploaded successfully via XTMT.")
                db.record_evidence_result(
                    job_id, evidence_id, success=True, file_hash=result["pre_upload_sha256"]
                )
            else:
                logger.error(f"Evidence {evidence_id} XTMT upload NOT confirmed successful: {result}")
                db.record_evidence_result(job_id, evidence_id, success=False)

        except XtmtCaseNameError as e:
            logger.error(f"Evidence {evidence_id}: case number format issue: {e}")
            db.record_evidence_result(job_id, evidence_id, success=False)

        except XtmtError as e:
            logger.error(f"Evidence {evidence_id}: XTMT error: {e}")
            db.record_evidence_result(job_id, evidence_id, success=False)

        except Exception:
            logger.exception(f"Unexpected error processing evidence {evidence_id}")
            db.record_evidence_result(job_id, evidence_id, success=False)

        finally:
            if local_path and os.path.exists(local_path):
                os.remove(local_path)
                logger.info(f"Cleaned up local staging file: {local_path}")

    job = db.get_job(job_id)
    total = job.get("totalEvidence", len(evidence_list))
    synced = job.get("syncedEvidence", 0)
    failed = job.get("failedEvidence", [])

    if synced == total and not failed:
        final_status = "SUCCESS"
    elif synced == 0:
        final_status = "FAILED"
    else:
        final_status = "PARTIAL"

    db.update_status(job_id, final_status, completedAt=job["updatedAt"])
    logger.info(f"Job {job_id} finalized as {final_status} ({synced}/{total} synced)")


def run_forever():
    logger.info("=" * 70)
    logger.info("XTMT Worker (Windows EC2) -- SOLE upload path for the DEMS -> Axon pipeline")
    logger.info(f"Queue: {SQS_QUEUE_URL}")
    logger.info("=" * 70)

    logger.info("Ensuring Upload XT is logged in before starting...")
    ensure_login()
    logger.info("Login check complete. Beginning to poll for jobs.")

    jobs_processed = 0

    while True:
        try:
            response = sqs.receive_message(
                QueueUrl=SQS_QUEUE_URL,
                MaxNumberOfMessages=MAX_MESSAGES_PER_POLL,
                WaitTimeSeconds=POLL_WAIT_TIME_SECONDS,
                VisibilityTimeout=VISIBILITY_TIMEOUT_SECONDS,
            )
            messages = response.get("Messages", [])

            if not messages:
                continue

            for msg in messages:
                receipt_handle = msg["ReceiptHandle"]
                try:
                    body = json.loads(msg["Body"])
                    _process_job(body)

                    sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=receipt_handle)
                    logger.info("Message processed and removed from queue.")
                    jobs_processed += 1

                    if jobs_processed % LOGIN_CHECK_EVERY_N_JOBS == 0:
                        logger.info("Periodic login re-check...")
                        ensure_login()

                except Exception:
                    logger.exception("Unhandled error processing message -- leaving in queue for redrive.")

        except Exception:
            logger.exception("Error in polling loop -- retrying in 10s.")
            time.sleep(10)


if __name__ == "__main__":
    run_forever()
