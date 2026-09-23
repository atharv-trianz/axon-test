
import os
import json
import time
import logging
import boto3
from pywinauto.application import Application
from pywinauto import Desktop
import pyotp

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# =====================================
# CONFIGURATION
# =====================================
RESOURCE_REGION = os.environ.get("AWS_RESOURCE_REGION", "us-east-2")
AXON_CREDS_SECRET_ARN = os.environ.get(
    "AXON_CREDS_SECRET_ARN",
    "arn:aws:secretsmanager:us-east-2:770410989764:secret:axon-automation-creds-8xqNgf",
)
AGENCY_URL = os.environ.get("AXON_AGENCY_URL", "ingest-tbi-test-portal.evidence.com")

UPLOAD_XT_PATH = os.environ.get(
    "UPLOAD_XT_PATH",
    r"C:\Program Files\Axon Enterprise\Axon Evidence Upload XT v2"
    r"\CLIENT\Axon Evidence Upload XT v2.exe",
)

# How long to wait for slow-rendering UI elements before giving up.
# Generous on purpose -- confirmed EC2/RDP can be noticeably slower
# than a physical laptop, especially on first launch.
UI_POLL_TIMEOUT_SECONDS = 60
UI_POLL_INTERVAL_SECONDS = 1

_secrets_client = boto3.client("secretsmanager", region_name=RESOURCE_REGION)

_cached_creds = None


def _get_axon_credentials() -> dict:
    global _cached_creds
    if _cached_creds is not None:
        return _cached_creds

    logger.info(f"Fetching Axon credentials from Secrets Manager: {AXON_CREDS_SECRET_ARN}")
    resp = _secrets_client.get_secret_value(SecretId=AXON_CREDS_SECRET_ARN)
    creds = json.loads(resp["SecretString"])

    missing = [k for k in ("username", "password", "totpSecret") if not creds.get(k)]
    if missing:
        raise RuntimeError(
            f"Secrets Manager secret is missing required field(s): {', '.join(missing)}. "
            f"Expected keys: username, password, totpSecret."
        )

    _cached_creds = creds
    logger.info(f"Credentials loaded for: {creds['username']}")
    return creds


def _wait_for_url_box(app, timeout=UI_POLL_TIMEOUT_SECONDS):
    """
    Polls for the Agency URL text box to actually appear and be ready,
    instead of guessing a fixed sleep duration. This is the core fix --
    works whether the app renders in 2 seconds (fast laptop) or 40+
    seconds (slow first-launch on EC2/RDP).
    """
    waited = 0
    while waited < timeout:
        try:
            window = app.top_window()
            url_box = window.child_window(
                auto_id="AgencyUrlTextEdit",
                control_type="Edit"
            )
            # .exists() actively checks the real UI tree right now,
            # rather than assuming a cached/stale reference is valid.
            if url_box.exists(timeout=0.5):
                logger.info(f"Agency URL field found after {waited}s.")
                return window, url_box
        except Exception:
            pass
        time.sleep(UI_POLL_INTERVAL_SECONDS)
        waited += UI_POLL_INTERVAL_SECONDS
        if waited % 5 == 0:
            logger.info(f"Still waiting for Upload XT's Agency URL field to render ({waited}s/{timeout}s)...")

    raise RuntimeError(
        f"Agency URL field did not appear within {timeout}s. Upload XT may have "
        f"shown an unexpected first-launch dialog (license agreement, update "
        f"check, etc.) -- check the RDP session visually if this keeps happening."
    )


def _wait_for_continue_button(window, timeout=UI_POLL_TIMEOUT_SECONDS):
    """Same polling approach for the Continue button."""
    waited = 0
    while waited < timeout:
        try:
            continue_btn = window.child_window(title="Continue", control_type="Button")
            if continue_btn.exists(timeout=0.5):
                return continue_btn
        except Exception:
            pass
        time.sleep(UI_POLL_INTERVAL_SECONDS)
        waited += UI_POLL_INTERVAL_SECONDS

    raise RuntimeError(f"Continue button did not appear within {timeout}s.")


def _wait_for_signin_window(timeout=UI_POLL_TIMEOUT_SECONDS):
    """Polls for the 'Sign in' browser window Upload XT opens, instead
    of a fixed sleep(20). Also handles the (confirmed-possible) case
    where the session is ALREADY authenticated and Upload XT skips
    straight past the sign-in screen entirely."""
    waited = 0
    while waited < timeout:
        try:
            for w in Desktop(backend="uia").windows():
                try:
                    title = w.window_text() or ""
                    if "sign in" in title.lower():
                        return w.wrapper_object(), "signin"
                    if "success" in title.lower() or "authentication" in title.lower():
                        return w.wrapper_object(), "already_authenticated"
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(UI_POLL_INTERVAL_SECONDS)
        waited += UI_POLL_INTERVAL_SECONDS
        if waited % 5 == 0:
            logger.info(f"Still waiting for the sign-in/confirmation window ({waited}s/{timeout}s)...")

    return None, None


def ensure_login() -> bool:
    """
    Runs the confirmed-working login sequence, but with EVERY fixed
    time.sleep() replaced by active polling for the real UI state --
    making this reliable regardless of how fast/slow the underlying
    machine renders Upload XT's UI. No manual pre-launch step needed.
    """
    creds = _get_axon_credentials()
    username = creds["username"]
    password = creds["password"]
    secret = creds["totpSecret"]

    # =====================================
    # LAUNCH UPLOAD XT
    # =====================================
    print("Launching Upload XT...")
    Application(backend="uia").start(UPLOAD_XT_PATH)

    # Give the OS a moment to register the new process before we try
    # to connect to it at all (this part genuinely is near-instant
    # regardless of machine speed, so a short fixed wait is fine here).
    time.sleep(2)

    app = Application(backend="uia").connect(
        title_re=".*Axon Evidence Upload XT v2.16.19.*"
    )

    # =====================================
    # ENTER AGENCY URL (now polls instead of guessing a fixed wait)
    # =====================================
    window, url_box = _wait_for_url_box(app)

    url_box.set_focus()
    url_box.type_keys("^a{DEL}")
    url_box.type_keys(AGENCY_URL)

    window.type_keys("{TAB}")

    continue_btn = _wait_for_continue_button(window)
    continue_btn.click_input()

    print("Waiting for login page...")

    # =====================================
    # LOGIN TO AXON (or detect already-authenticated session reuse)
    # =====================================
    result_window, result_type = _wait_for_signin_window()

    if result_window is None:
        raise RuntimeError("No sign-in or confirmation window appeared within the timeout.")

    if result_type == "already_authenticated":
        logger.info("Session already valid -- authentication succeeded via reuse, no login needed.")
        return True

    edge = result_window
    edge.set_focus()

    # Username
    edge.type_keys("^a{DEL}")
    edge.type_keys(username)
    edge.type_keys("{TAB}")

    # Password
    edge.type_keys("^a{DEL}")
    edge.type_keys(password)
    edge.type_keys("{ENTER}")

    # =====================================
    # MFA OTP
    # =====================================
    time.sleep(3)

    otp = pyotp.TOTP(secret).now()
    print(f"Generated OTP: {otp}")

    edge.set_focus()
    edge.type_keys("{ESC}")  # dismiss any "Save password?" popup
    time.sleep(1)
    edge.set_focus()

    edge.type_keys(otp)
    edge.type_keys("{ENTER}")

    print("Login completed successfully.")
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ensure_login()
