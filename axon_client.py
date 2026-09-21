"""
axon_client.py — Nova Act Workflow-based client for the Axon Justice
Ingest Portal. WINDOWS DEMO VERSION.

============================================================================
FIX (this revision): ActResult attribute name
============================================================================
Previous crash:
    AttributeError: 'ActResult' object has no attribute 'response'
This happened AFTER login + MFA succeeded (workflow run status showed
'SUCCEEDED') — the .act() call itself worked fine, but this specific
nova-act SDK version's result object does not expose the text output
under a `.response` attribute the way earlier assumptions expected.

FIX: `_get_act_text()` below tries several common attribute names used
across different nova-act SDK versions/result types. If NONE of them
match, it logs the object's actual type and every available attribute
name to CloudWatch/console, so the correct one can be identified in a
single follow-up run instead of another guess-and-rebuild cycle.
============================================================================

CONNECTION PATTERN (confirmed working):
    workflow = Workflow(workflow_definition_name=WORKFLOW_NAME, model_id="nova-act-latest")
    workflow.__enter__()
    nova = NovaAct(workflow=workflow, starting_page=..., ...)

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

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
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
    pass


# ============================================================================
# DEFENSIVE RESULT-TEXT EXTRACTOR
# ============================================================================
_COMMON_RESULT_ATTR_NAMES = (
    "response", "text", "result", "output", "answer",
    "message", "value", "content", "data", "reply",
)


def _get_act_text(act_result) -> str:
    """Safely extracts the natural-language text from a nova.act() result
    object, regardless of which attribute name this SDK version actually
    uses for it. Tries every common name seen across nova-act versions;
    if none match, logs full diagnostic info (type + all attributes) so
    the correct attribute name can be identified immediately from the
    next run's output."""
    for attr_name in _COMMON_RESULT_ATTR_NAMES:
        if hasattr(act_result, attr_name):
            val = getattr(act_result, attr_name)
            if isinstance(val, str) and val:
                return val

    # None of the common names worked. Dump full diagnostics so the next
    # run's log tells us EXACTLY what to use -- no more guessing.
    logger.error(f"[DIAGNOSTIC] Could not find text attribute on result. Type: {type(act_result)}")
    all_attrs = [a for a in dir(act_result) if not a.startswith("_")]
    logger.error(f"[DIAGNOSTIC] Available attributes: {all_attrs}")
    try:
        logger.error(f"[DIAGNOSTIC] repr(result): {repr(act_result)}")
    except Exception:
        pass
    try:
        logger.error(f"[DIAGNOSTIC] vars(result): {vars(act_result)}")
    except Exception:
        pass

    # Last resort: try str() of the whole object rather than crashing,
    # so the calling code can still make a best-effort decision (e.g.
    # checking substrings) even in the worst case.
    try:
        return str(act_result)
    except Exception:
        return ""


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
    response_text = _get_act_text(result).lower()
    logger.info(f"Login step result text: {response_text[:200]}")
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
    mfa_response = _get_act_text(mfa_result).lower()
    logger.info(f"MFA step result text: {mfa_response[:200]}")

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
    retry_response = _get_act_text(retry_result).lower()
    logger.info(f"MFA retry result text: {retry_response[:200]}")

    if "evidence" in retry_response or "success" in retry_response or "dashboard" in retry_response:
        logger.info("MFA verification succeeded on retry.")
        return

    # Even if we can't confirm via text keywords, don't crash here --
    # just proceed and let the upload step's own confirmation logic be
    # the real judge of whether we're actually logged in.
    logger.warning(
        "Could not confirm MFA success via result text, but proceeding "
        "to upload step anyway (it has its own independent confirmation)."
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
        resp_lower = _get_act_text(check).lower()
        logger.info(f"Attempt {attempt + 1}/12 — progress: {resp_lower[:150]}")
        if "complet" in resp_lower or "done" in resp_lower or "100%" in resp_lower or "success" in resp_lower:
            logger.info(f"Upload confirmed complete on attempt {attempt + 1}.")
            break

    confirm = nova.act(
        f"Report whether a file titled or matching '{evidence_title}' is now "
        f"visible in the evidence list."
    )
    confirm_text = _get_act_text(confirm)
    found = bool(confirm_text) and "not" not in confirm_text.lower()

    if found:
        logger.info(f"Confirmed '{evidence_title}' is now visible.")
        return True

    logger.warning(f"Could not confirm '{evidence_title}' after upload.")
    return False


def sync_evidence_to_axon(username: str, password: str, local_path: str,
                           evidence_title: str, totp_secret: str = None) -> bool:
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
            headless=False,
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
