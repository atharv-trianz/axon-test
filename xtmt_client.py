"""
xtmt_client.py - Automates Axon's XTMT.exe (bundled inside Upload XT v2)
for ALL evidence uploads, any file size. This is now the SOLE upload
mechanism for the DEMS -> Axon pipeline (Nova Act / browser automation
has been fully removed from this architecture).

============================================================================
CONFIRMED WORKING FLAGS (validated via many isolated test runs,
22-24 Sep 2026, against ingest-tbi-test-portal.evidence.com):
============================================================================
  -o <operation name>   (required)
  -p <file path>         (required)
  -c <case number>       (works for both existing AND new cases)
  -n                     (create case if new)
  -a                     (autoRun - skips the Y/N prompt)
  -t <category>          (optional)
  -b <batch size>        (optional)

  Case number format: real DEMS case numbers use "YYYY-XXXXXXX"
  (e.g. "2026-1222112"). This client validates that format before
  calling XTMT and refuses to proceed otherwise.

============================================================================
CRITICAL FIX - CONSOLE INHERITANCE (confirmed root cause, 24 Sep 2026):
============================================================================
Every command typed DIRECTLY into cmd/PowerShell succeeded, 100% of the
time. Every command launched PROGRAMMATICALLY with captured/redirected
stdout FAILED, 100% of the time -- even using the exact same case
number, file, and flags that had just succeeded manually seconds
earlier.

XTMT prints a live-updating progress bar:
    [XXXXXXXXXX]  1/1  100%  C:\\path\\to\\file.pdf
This requires direct console cursor manipulation (.NET's
Console.SetCursorPosition or similar). That call THROWS when stdout is
redirected into a pipe instead of a real console -- exactly what
capturing stdout does. The thrown exception is what gets surfaced as
the generic "Error occurred while executing XTMT application
migration."

FIX: do NOT capture or redirect XTMT's stdout/stderr. Let it inherit
the calling process's real console. Verify success afterward by
reading XTMT's own Progress log file instead -- its path is fully
deterministic based on the operation name:
    %LOCALAPPDATA%\\Axon\\UploadXT\\XTMT\\Logs\\<operation>\\Progress_<operation>.tsv

IMPORTANT IMPLICATION FOR THE WORKER: because this worker process's own
console is what gets inherited, xtmt_worker.py MUST be run as a normal
interactive console application (e.g. via Task Scheduler with "Run only
when user is logged on", or inside a persistent RDP/console session) --
NOT as a Windows Service or a fully detached/headless background
process, since those do not have a real console for XTMT to inherit.
============================================================================

LOGIN: XTMT requires NO separate login of its own -- it rides on
whatever session the Upload XT v2 desktop app already has open. See
ensure_uploadxt_login.py for how that session is established/verified
before any upload is attempted.
============================================================================
"""
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
    required 'YYYY-XXXXXXX' format -- confirmed this format is required
    for reliable case creation/matching, and non-standard formats have
    been observed to fail silently."""
    pass


def validate_case_number_format(case_number: str):
    if not CASE_NUMBER_PATTERN.match(case_number):
        raise XtmtCaseNameError(
            f"Case number '{case_number}' does not match the confirmed-working "
            f"format 'YYYY-XXXXXXX' (e.g. '2026-1222112'). Refusing to proceed -- "
            f"this exact scenario caused a silent failure during testing."
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
    success/failure. This is the verification mechanism used INSTEAD of
    parsing live console output, since capturing that output is what
    broke XTMT's console-drawing logic in the first place (see module
    docstring).
    """
    result = {
        "log_found": False,
        "raw_rows": [],
        "likely_success": None,
        "fail_keywords_found": [],
    }

    if not os.path.exists(progress_log_path):
        logger.warning(f"Progress log not found at expected path: {progress_log_path}")
        return result

    result["log_found"] = True

    with open(progress_log_path, "r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f, delimiter="\t")
        rows = list(reader)

    result["raw_rows"] = rows
    full_text = "\n".join(["\t".join(r) for r in rows]).lower()
    fail_terms = ["fail", "error", "exception", "denied", "reject"]
    found_fail_terms = [t for t in fail_terms if t in full_text]
    result["fail_keywords_found"] = found_fail_terms

    # Conservative signal: log exists, has content, no failure keywords.
    result["likely_success"] = bool(rows) and not found_fail_terms

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
    isolated testing, and WITHOUT capturing XTMT's stdout/stderr (see
    module docstring for why this is required).

    Returns a dict with: success (bool), exit_code, progress_log (dict),
    pre_upload_sha256, operation_name.

    Raises XtmtCaseNameError if case_number isn't in the confirmed
    -working format, or XtmtError for any other real failure (file
    missing, XTMT.exe missing, timeout).
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
    logger.info("XTMT output will print directly to this process's own console "
                "(not captured), so its progress bar can draw correctly.")

    try:
        proc = subprocess.run(
            args,
            cwd=xtmt_working_dir,
            timeout=timeout_seconds,
            # Deliberately NOT setting stdout/stderr -- letting XTMT
            # inherit this process's real console is the confirmed fix.
        )
    except subprocess.TimeoutExpired as e:
        raise XtmtError(f"XTMT timed out after {timeout_seconds}s for case {case_number}") from e

    logger.info(f"XTMT exit code: {proc.returncode}")

    progress_log_path = os.path.expandvars(
        rf"%LOCALAPPDATA%\Axon\UploadXT\XTMT\Logs\{operation_name}\Progress_{operation_name}.tsv"
    )
    parsed = _parse_progress_log(progress_log_path)

    success = (proc.returncode == 1000) and (parsed["likely_success"] is True)

    if not success:
        logger.error(
            f"XTMT upload NOT confirmed successful for case {case_number}. "
            f"exit_code={proc.returncode}, progress_log_found={parsed['log_found']}, "
            f"fail_keywords={parsed['fail_keywords_found']}"
        )

    return {
        "success": success,
        "exit_code": proc.returncode,
        "progress_log": parsed,
        "pre_upload_sha256": pre_upload_hash,
        "operation_name": operation_name,
    }
