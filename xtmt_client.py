
import os
import re
import csv
import subprocess
import hashlib
import logging
from datetime import datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)

XTMT_PATH = os.environ.get(
    "XTMT_PATH",
    r"C:\Program Files\Axon Enterprise\Axon Evidence Upload XT v2\XTMT\XTMT.exe",
)
DEFAULT_CATEGORY = os.environ.get("AXON_DEFAULT_CATEGORY", "90-Day Retention")

CASE_NUMBER_PATTERN = re.compile(r"^\d{4}-\d{6,8}$")


class XtmtError(RuntimeError):
    """Raised when XTMT genuinely fails (not found, timed out, or its
    own progress log indicates a real failure)."""
    pass


class XtmtCaseNameError(XtmtError):
    """Raised specifically when the case number doesn't match the
    required 'YYYY-XXXXXXX' format."""
    pass


def validate_case_number_format(case_number: str):
    if not CASE_NUMBER_PATTERN.match(case_number):
        raise XtmtCaseNameError(
            f"Case number '{case_number}' does not match the confirmed-working "
            f"format 'YYYY-XXXXXXX' (e.g. '2026-1222112')."
        )


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_progress_log(progress_log_path: str) -> dict:
    """
    Reads XTMT's own Progress_<operation>.tsv log file to determine REAL
    success/failure.

    FIX (24 Sep 2026): parses the TSV PROPERLY using the header row to
    locate the actual columns (ItemUploadStatus, ErrorCode, ErrorMessage)
    and checks the real VALUES in those columns for each data row --
    instead of the old blind keyword search across the whole raw text,
    which incorrectly matched the word "error" appearing only in the
    column HEADER NAMES ("ErrorCode", "ErrorMessage"), even when those
    columns were correctly empty for a fully successful upload.
    """
    result = {
        "log_found": False,
        "raw_rows": [],
        "likely_success": None,
        "data_row_statuses": [],
        "data_row_errors": [],
    }

    if not os.path.exists(progress_log_path):
        logger.warning(f"Progress log not found at expected path: {progress_log_path}")
        return result

    result["log_found"] = True

    with open(progress_log_path, "r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f, delimiter="\t")
        rows = list(reader)

    result["raw_rows"] = rows

    if not rows:
        result["likely_success"] = False
        return result

    header = rows[0]
    data_rows = rows[1:]

    if not data_rows:
        # Header present but no actual data rows -- nothing was
        # recorded as processed, treat as not confirmed successful.
        logger.warning("Progress log has a header row but no data rows.")
        result["likely_success"] = False
        return result

    # Locate the real column indexes by NAME, rather than assuming a
    # fixed position -- this is robust even if XTMT changes column
    # order in a future version.
    def _col_index(col_name: str):
        for i, h in enumerate(header):
            if h.strip().lower() == col_name.strip().lower():
                return i
        return None

    status_idx = _col_index("ItemUploadStatus")
    error_code_idx = _col_index("ErrorCode")
    error_message_idx = _col_index("ErrorMessage")

    all_rows_ok = True

    for row in data_rows:
        status_val = row[status_idx].strip() if status_idx is not None and status_idx < len(row) else ""
        error_code_val = row[error_code_idx].strip() if error_code_idx is not None and error_code_idx < len(row) else ""
        error_message_val = row[error_message_idx].strip() if error_message_idx is not None and error_message_idx < len(row) else ""

        result["data_row_statuses"].append(status_val)
        result["data_row_errors"].append({"code": error_code_val, "message": error_message_val})

        # A row is only a real failure if it has an actual non-empty
        # ErrorCode/ErrorMessage VALUE, or an explicit non-success
        # status. "Done" (confirmed from a real successful run) is
        # treated as success. Being conservative: if we don't recognize
        # the status text at all, we do NOT assume success blindly --
        # we only trust rows that are unambiguously "Done" with no
        # error values populated.
        row_has_error_value = bool(error_code_val) or bool(error_message_val)
        row_status_ok = status_val.lower() in ("done", "success", "completed", "uploaded")

        if row_has_error_value or not row_status_ok:
            all_rows_ok = False

    result["likely_success"] = all_rows_ok
    return result


def upload_evidence_via_xtmt(
    case_number: str,
    local_file_path: str,
    operation_name: str = None,
    category: str = None,
    description: str = None,
    batch_size: int = None,
    timeout_seconds: int = 1800,
) -> dict:
    """
    Uploads one evidence file into Axon via XTMT's command-line
    interface, using ONLY the flag combination confirmed safe through
    isolated testing, and WITHOUT capturing XTMT's stdout/stderr.

    Returns a dict with: success (bool), exit_code, progress_log (dict),
    pre_upload_sha256, operation_name.
    """
    validate_case_number_format(case_number)

    if not os.path.exists(local_file_path):
        raise XtmtError(f"File not found: {local_file_path}")

    if not os.path.exists(XTMT_PATH):
        raise XtmtError(f"XTMT.exe not found at: {XTMT_PATH}")

    operation_name = operation_name or f"sync-{case_number}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    category = category or DEFAULT_CATEGORY

    pre_upload_hash = file_sha256(local_file_path)
    logger.info(f"Pre-upload SHA-256 for '{local_file_path}': {pre_upload_hash}")

    args = [
        XTMT_PATH,
        "-o", operation_name,
        "-p", local_file_path,
        "-c", case_number,
        "-a",
        "-n",
    ]
    if category:
        args += ["-t", category]
    if batch_size:
        args += ["-b", str(batch_size)]
    if description:
        args += ["-ds", description]

    xtmt_working_dir = os.path.dirname(XTMT_PATH)
    logger.info(f"Running XTMT (cwd={xtmt_working_dir}): {' '.join(args)}")
    logger.info("XTMT output will print directly to this process's own console.")

    try:
        proc = subprocess.run(
            args,
            cwd=xtmt_working_dir,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as e:
        raise XtmtError(f"XTMT timed out after {timeout_seconds}s for case {case_number}") from e

    logger.info(f"XTMT exit code: {proc.returncode}")

    progress_log_path = os.path.expandvars(
        rf"%LOCALAPPDATA%\Axon\UploadXT\XTMT\Logs\{operation_name}\Progress_{operation_name}.tsv"
    )
    parsed = _parse_progress_log(progress_log_path)

    success = (proc.returncode == 1000) and (parsed["likely_success"] is True)

    if success:
        logger.info(
            f"XTMT upload CONFIRMED successful for case {case_number}. "
            f"Row statuses: {parsed['data_row_statuses']}"
        )
    else:
        logger.error(
            f"XTMT upload NOT confirmed successful for case {case_number}. "
            f"exit_code={proc.returncode}, progress_log_found={parsed['log_found']}, "
            f"row_statuses={parsed['data_row_statuses']}, row_errors={parsed['data_row_errors']}"
        )

    return {
        "success": success,
        "exit_code": proc.returncode,
        "progress_log": parsed,
        "pre_upload_sha256": pre_upload_hash,
        "operation_name": operation_name,
    }
