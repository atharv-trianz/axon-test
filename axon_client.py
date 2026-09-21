"""
axon_client.py — Nova Act Workflow-based client for the Axon Justice
Ingest Portal. WINDOWS VERSION for today's demo — combines:

  1. The PROVEN connection pattern from Pranjal's working script:
         workflow = Workflow(workflow_definition_name=..., model_id=...)
         workflow.__enter__()
         nova = NovaAct(workflow=workflow, ...)
  2. headless=False — a REAL VISIBLE browser window, so the Teams call
     can watch the login + MFA + upload happen live.
  3. AUTOMATED MFA via pyotp — this is the feature being demoed. Instead
     of a human reading their Authenticator app and typing the code
     (Pranjal's original script used input() for this), this script
     generates the code itself from the same secret and types it in.

This plugs into the REAL pipeline: sqs_worker.py (in this same package)
pulls real jobs from the same SQS queue the Trigger Lambda already
writes to, so clicking "Move to Axon" in DEMS during the call flows
through the actual Lambda -> SQS -> (this script) -> DynamoDB path,
not a disconnected standalone demo.

FLOW (matches real portal screenshots):
  1. Login (Email/Username + Password + Sign In)
  2. MFA (Microsoft Authenticator, 6-digit code) — AUTOMATED via pyotp
  3. Lands on "Evidence" page (default landing page after login)
  4. Click "+ Import Evidence" -> Choose Files -> Upload
  5. Poll Progress column, confirm via Evidence list
"""
import os
import time
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Nova Act's Workflow service only exists in us-east-1 — this is
# independent of where the evidence/queue/database resources live
# (us-east-2), and independent of which machine runs this script.
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Matches Pranjal's own proven working browser args (window size tuned
# for a visible, presentable demo window rather than the minimal flags
# used in the headless Linux/EC2 version).
os.environ.setdefault(
    "NOVA_ACT_BROWSER_ARGS",
    "--remote-debugging-port=9222 --window-size=1600,813"
)

from nova_act import NovaAct, Workflow, SecurityOptions

try:
    import pyotp
    _PYOTP_AVAILABLE = True
except ImportError:
    _PYOTP_AVAILABLE = False

AXON_PORTAL_URL = os.environ.get(
    "AXON_PORTAL_URL",
    "https://ingest-tbi-test-portal.evidence.com/axon/evidence-search",
)
WORKFLOW_NAME = os.environ.get("NOVA_ACT_WORKFLOW_NAME", "axon-sync-workflow")


class MfaRequiredError(RuntimeError):
    """Raised only when NO totp_secret is configured and Axon presents an
    MFA/2FA challenge this worker cannot answer."""
    pass


def _generate_totp_code(totp_secret: str) -> str:
    code = pyotp.TOTP(totp_secret).now()
    logger.info("Generated TOTP code locally (no phone involved).")
    return code


def _do_login(nova, username: str, password: str, totp_secret: str = None):
    logger.info("Logging in to Axon Ingest Portal...")
    result = nova.act(
        f"Type '{username}' into the Email or Username field. "
        f"Type '{password}' into the Password field. "
        f"Click the Sign In button. If an Authenticator or "
        f"Multi-factor authentication code screen appears, do NOT "
        f"attempt to guess or enter anything — just report that an "
        f"MFA code is requested."
    )
    response_text = (result.response or "").lower()
    mfa_prompted = any(term in response_text for term in ("mfa", "authenticator", "authentication code", "multi-factor"))

    if not mfa_prompted:
        logger.info("Login step completed — no MFA prompt encountered.")
        return

    if not totp_secret:
        raise MfaRequiredError(
            "Axon presented an MFA/Authenticator prompt, and no totp_secret "
            "was configured for this automation account."
        )
    if not _PYOTP_AVAILABLE:
        raise RuntimeError("totp_secret was provided but 'pyotp' is not installed.")

    logger.info("MFA prompt detected — generating TOTP code automatically (no phone).")
    code = _generate_totp_code(totp_secret)
    mfa_result = nova.act(
        f"Type '{code}' into the Authentication Code field. "
        f"Click Continue or Verify. Report whether login succeeded."
    )
    mfa_response = (mfa_result.response or "").lower()

    if "evidence" in mfa_response or "success" in mfa_response or "dashboard" in mfa_response:
        logger.info("MFA verification succeeded using auto-generated TOTP code.")
        return

    logger.warning("First TOTP attempt unclear — retrying once with a fresh code.")
    time.sleep(2)
    retry_code = _generate_totp_code(totp_secret)
    retry_result = nova.act(
        f"If still on the MFA/Authentication Code screen, clear the field "
        f"and type '{retry_code}', then click Continue. Report whether "
        f"login succeeded."
    )
    retry_response = (retry_result.response or "").lower()
    if "evidence" in retry_response or "success" in retry_response or "dashboard" in retry_response:
        logger.info("MFA verification succeeded on retry.")
        return

    raise MfaRequiredError(
        "TOTP code was generated and submitted, but login could not be "
        "confirmed successful after 2 attempts. Verify the TOTP_SECRET value."
    )


def _do_upload(nova, local_path: str, evidence_title: str) -> bool:
    abs_path = os.path.abspath(local_path)

    logger.info("Clicking '+Import Evidence'...")
    nova.act("Click the +Import Evidence button in the top right.")

    logger.info(f"Selecting file via Choose Files: {abs_path}")
    nova.act(f"In the drag-and-drop area, click Choose files and select the file at '{abs_path}'.")
    time.sleep(2)

    logger.info("Clicking 'Upload'...")
    nova.act("Click the Upload button.")

    logger.info("Polling upload progress...")
    for attempt in range(12):
        time.sleep(5)
        check = nova.act(
            "Report the current upload progress or status shown for this file "
            "(e.g. 'in progress', 'uploading', 'completed')."
        )
        resp_lower = (check.response or "").lower()
        if "complet" in resp_lower or "done" in resp_lower or "100%" in resp_lower or "success" in resp_lower:
            logger.info(f"Upload confirmed complete on attempt {attempt + 1}.")
            break
        logger.info(f"Attempt {attempt + 1}/12 — progress: {check.response}")

    confirm = nova.act(
        f"Report whether a file titled or matching '{evidence_title}' is now "
        f"visible in the evidence list."
    )
    found = confirm.response and "not" not in confirm.response.lower()

    if found:
        logger.info(f"Confirmed '{evidence_title}' is now visible.")
        return True

    logger.warning(f"Could not confirm '{evidence_title}' after upload.")
    return False


def sync_evidence_to_axon(username: str, password: str, local_path: str,
                           evidence_title: str, totp_secret: str = None) -> bool:
    """
    Runs one full Axon sync session: login -> MFA (automated) -> upload
    one evidence file -> confirm. Returns True if upload was confirmed.
    Raises MfaRequiredError if MFA cannot be resolved.

    headless=False on purpose — this is the DEMO version, meant to be
    watched live on the Teams call.
    """
    workflow_ctx = None
    nova = None
    try:
        workflow_ctx = Workflow(workflow_definition_name=WORKFLOW_NAME, model_id="nova-act-latest")
        workflow_ctx.__enter__()
        logger.info(f"Started workflow session: {WORKFLOW_NAME}")

        nova = NovaAct(
            workflow=workflow_ctx,
            starting_page=AXON_PORTAL_URL,
            ignore_https_errors=True,
            headless=False,  # VISIBLE for the demo call
            security_options=SecurityOptions(allowed_file_upload_paths=["*"]),
            tty=False,
        )
        nova.start()
        logger.info("Nova Act browser started — visible window for the demo.")

        _do_login(nova, username, password, totp_secret)
        return _do_upload(nova, local_path, evidence_title)

    finally:
        if nova is not None:
            try:
                nova.stop()
                logger.info("Nova Act browser stopped.")
            except Exception:
                logger.exception("Non-fatal error stopping Nova Act session.")
        if workflow_ctx is not None:
            try:
                workflow_ctx.__exit__(None, None, None)
                logger.info("Workflow session closed.")
            except Exception:
                logger.exception("Non-fatal error closing workflow session.")
