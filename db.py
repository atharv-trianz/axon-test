"""
db.py — DynamoDB helper for the AxonSyncJobs table.
Identical to the version used everywhere else in this project — the
data layer doesn't change based on which compute runs the worker logic.

NOTE: updatedAt is stored as a STRING (matches the GSI's String type).
"""
import os
import time
import boto3
from boto3.dynamodb.conditions import Key

RESOURCE_REGION = os.environ.get("AWS_RESOURCE_REGION", "us-east-2")
TABLE_NAME = os.environ.get("AXON_SYNC_TABLE", "AxonSyncJobs")

_dynamodb = boto3.resource("dynamodb", region_name=RESOURCE_REGION)
_table = _dynamodb.Table(TABLE_NAME)


def _now_str() -> str:
    return str(int(time.time()))


def get_job(job_id: str):
    resp = _table.get_item(Key={"jobId": job_id})
    return resp.get("Item")


def get_previous_hash_log(case_number: str):
    resp = _table.query(
        IndexName="caseNumber-updatedAt-index",
        KeyConditionExpression=Key("caseNumber").eq(case_number),
        ScanIndexForward=False,
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0]["evidenceHashLog"] if items else {}


def update_status(job_id: str, status: str, **kwargs):
    expr_names = {"#s": "status", "#u": "updatedAt"}
    expr_values = {":s": status, ":u": _now_str()}
    set_parts = ["#s = :s", "#u = :u"]

    for k, v in kwargs.items():
        expr_names[f"#{k}"] = k
        expr_values[f":{k}"] = v
        set_parts.append(f"#{k} = :{k}")

    _table.update_item(
        Key={"jobId": job_id},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def record_evidence_result(job_id: str, evidence_id: str, success: bool, file_hash: str = None):
    if success:
        _table.update_item(
            Key={"jobId": job_id},
            UpdateExpression=(
                "SET syncedEvidence = syncedEvidence + :one, "
                "evidenceHashLog.#eid = :hash, updatedAt = :u"
            ),
            ExpressionAttributeNames={"#eid": evidence_id},
            ExpressionAttributeValues={":one": 1, ":hash": file_hash or "", ":u": _now_str()},
        )
    else:
        _table.update_item(
            Key={"jobId": job_id},
            UpdateExpression=(
                "SET failedEvidence = list_append(failedEvidence, :eid), updatedAt = :u"
            ),
            ExpressionAttributeValues={":eid": [evidence_id], ":u": _now_str()},
        )
