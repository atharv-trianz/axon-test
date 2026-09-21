"""
sqs_worker.py — Windows Demo Worker, connected to the REAL pipeline.

This polls the SAME SQS queue (sync-to-axon-jobs) that the existing
Trigger Lambda already writes to. This means: clicking "Move to Axon" in
the real DEMS frontend during the call flows through the actual, already
-built API Gateway -> Trigger Lambda -> SQS path, and THIS script (running
on the Windows EC2 box) is simply the thing picking up that real message
and doing the Axon upload — with automated MFA, visible on screen.

WHY THIS MATTERS FOR THE DEMO:
  This is not a disconnected standalone script. It proves the FULL
  architecture end-to-end: real button click -> real Lambda -> real SQS
  -> this worker -> real DynamoDB status update -> real DEMS status
  badge changing to "Synced". Only difference from the final production
  target: this runs on a Windows box with a visible browser (for today's
  demo) instead of the headless Linux/EC2 service (the next milestone).

RUN THIS SCRIPT AND LEAVE IT RUNNING throughout the call. As soon as
someone clicks "Move to Axon" in DEMS, this window will spring to life.
"""
import os
import json
import time
import hashlib
import tempfile
import logging
import boto3

import db
from axon_client import sync_evidence_to_axon, MfaRequiredError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger()

# ----------------------------------------------------------------------
# Configuration — same values as the rest of the architecture. The
# queue/table/bucket/secret all live in us-east-2 (Ohio); only the Nova
# Act Workflow service call itself needs us-east-1 (handled inside
# axon_client.py).
# ----------------------------------------------------------------------
RESOURCE_REGION = os.environ.get("AWS_RESOURCE_REGION", "us-east-2")
SQS_QUEUE_URL = os.environ.get(
    "SYNC_QUEUE_URL",
    "https://sqs.us-east-2.amazonaws.com/770410989764/sync-to-axon-jobs",
)
AXON_CREDS_SECRET_ARN = os.environ.get(
    "AXON_CREDS_SECRET_ARN",
    "arn:aws:secretsmanager:us-east-2:770410989764:secret:axon-automation-creds-8xqNgf",
)
LARGE_FILE_THRESHOLD_BYTES = int(os.environ.get("LARGE_FILE_THRESHOLD_BYTES", 3_800_000_000))

POLL_WAIT_TIME_SECONDS = 20
MAX_MESSAGES_PER_POLL = 1
VISIBILITY_TIMEOUT_SECONDS = 960

sqs = boto3.client("sqs", region_name=RESOURCE_REGION)
secrets = boto3.client("secretsmanager", region_name=RESOURCE_REGION)
s3 = boto3.client("s3", region_name=RESOURCE_REGION)


def _get_axon_credentials():
    resp = secrets.get_secret_value(SecretId=AXON_CREDS_SECRET_ARN)
    return json.loads(resp["SecretString"])


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_evidence(bucket: str, key: str) -> str:
    local_path = os.path.join(tempfile.gettempdir(), os.path.basename(key))
    s3.download_file(bucket, key, local_path)
    return local_path


def _sync_with_retry(creds: dict, local_path: str, title: str, totp_secret: str,
                      max_attempts: int = 2) -> bool:
    last_exception = None
    for attempt in range(1, max_attempts + 1):
        try:
            return sync_evidence_to_axon(
                username=creds["username"],
                password=creds["password"],
                local_path=local_path,
                evidence_title=title,
                totp_secret=totp_secret,
            )
        except MfaRequiredError:
            raise
        except Exception as e:
            last_exception = e
            logger.warning(f"Attempt {attempt}/{max_attempts} failed for '{title}': {e}")
            if attempt < max_attempts:
                time.sleep(3)
    raise last_exception


def _process_job(message_body: dict):
    job_id = message_body["jobId"]
    case_number = message_body.get("caseNumber", "")
    evidence_list = message_body["evidence"]

    logger.info(f"=== Processing job {job_id} ({len(evidence_list)} evidence item(s)) ===")
    db.update_status(job_id, "IN_PROGRESS")
    previous_hashes = db.get_previous_hash_log(case_number) if case_number else {}

    routed_large = [e for e in evidence_list if e["sizeBytes"] >= LARGE_FILE_THRESHOLD_BYTES]
    routed_normal = [e for e in evidence_list if e["sizeBytes"] < LARGE_FILE_THRESHOLD_BYTES]

    for e in routed_large:
        logger.info(f"Evidence {e['evidenceId']} exceeds size threshold, flagging for XTMT large-file path.")
        db.record_evidence_result(job_id, e["evidenceId"], success=False)

    if not routed_normal:
        _finalize(job_id, len(evidence_list))
        return

    creds = _get_axon_credentials()
    totp_secret = creds.get("totpSecret")
    mfa_failed = False

    for e in routed_normal:
        evidence_id = e["evidenceId"]
        local_path = None
        try:
            local_path = _download_evidence(e["s3Bucket"], e["s3Key"])
            file_hash = _file_sha256(local_path)

            if previous_hashes.get(evidence_id) == file_hash:
                logger.info(f"Skipping already-synced evidence {evidence_id} (hash match)")
                db.record_evidence_result(job_id, evidence_id, success=True, file_hash=file_hash)
                continue

            success = _sync_with_retry(creds, local_path, e["title"], totp_secret)
            db.record_evidence_result(job_id, evidence_id, success=success,
                                        file_hash=file_hash if success else None)

        except MfaRequiredError as mfa_err:
            logger.error(f"Evidence {evidence_id} failed — MFA required: {mfa_err}")
            db.record_evidence_result(job_id, evidence_id, success=False)
            mfa_failed = True

        except Exception:
            logger.exception(f"Failed to sync evidence {evidence_id} for job {job_id}")
            db.record_evidence_result(job_id, evidence_id, success=False)

        finally:
            if local_path and os.path.exists(local_path):
                os.remove(local_path)

    if mfa_failed:
        db.update_status(job_id, "FAILED", errorDetails="MFA_REQUIRED")
        return

    _finalize(job_id, len(evidence_list))


def _finalize(job_id: str, total: int):
    job = db.get_job(job_id)
    synced = job.get("syncedEvidence", 0)
    failed = job.get("failedEvidence", [])

    if synced == total and not failed:
        final_status = "SUCCESS"
    elif synced == 0:
        final_status = "FAILED"
    else:
        final_status = "PARTIAL"

    db.update_status(job_id, final_status, completedAt=job["updatedAt"])
    logger.info(f"=== Job {job_id} finalized as {final_status} ({synced}/{total} synced) ===")


def run_forever():
    logger.info("=" * 70)
    logger.info("Windows Demo Worker — connected to REAL SQS/DynamoDB pipeline")
    logger.info(f"Queue: {SQS_QUEUE_URL}")
    logger.info("Waiting for a real 'Move to Axon' click from DEMS...")
    logger.info("=" * 70)

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
                except Exception:
                    logger.exception("Unhandled error processing message — leaving in queue for redrive.")

        except Exception:
            logger.exception("Error in polling loop — retrying in 10s.")
            time.sleep(10)


if __name__ == "__main__":
    run_forever()
