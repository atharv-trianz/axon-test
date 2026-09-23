"""
ensure_uploadxt_login.py

Based on the user's CONFIRMED WORKING manual login script -- same exact
sequence, same exact pywinauto pattern (.wrapper_object(), fixed sleeps,
direct type_keys()). The ONLY change: credentials (username, password,
totpSecret) are now fetched from AWS Secrets Manager instead of a local
.env file, so this matches how every other component in the pipeline
(Trigger Lambda, Worker, DB layer) already handles credentials.

Secret ARN: arn:aws:secretsmanager:us-east-2:770410989764:secret:axon-automation-creds-8xqNgf
Secret JSON shape:
    {
      "username": "atharv.pandey@trianz.com",
      "password": "...",
      "totpSecret": "JBSWY3DPEHPK3PXP"
    }
"""
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

_secrets_client = boto3.client("secretsmanager", region_name=RESOURCE_REGION)

# Cache fetched credentials in-process so ensure_login() (called
# periodically by xtmt_worker.py) doesn't hit Secrets Manager every time.
_cached_creds = None


def _get_axon_credentials() -> dict:
    """Fetches (and caches) username/password/totpSecret from Secrets
    Manager -- replaces the .env-based load_dotenv() approach."""
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


def ensure_login() -> bool:
    """
    Runs the EXACT confirmed-working login sequence, using credentials
    pulled from Secrets Manager instead of a .env file. Every step below
    mirrors the manually-tested script line for line.
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

    time.sleep(5)

    app = Application(backend="uia").connect(
        title_re=".*Axon Evidence Upload XT v2.16.19.*"
    )

    window = app.top_window()

    # =====================================
    # ENTER AGENCY URL
    # =====================================
    url_box = window.child_window(
        auto_id="AgencyUrlTextEdit",
        control_type="Edit"
    )

    url_box.set_focus()
    url_box.type_keys("^a{DEL}")
    url_box.type_keys(AGENCY_URL)

    window.type_keys("{TAB}")

    time.sleep(5)

    continue_btn = window.child_window(
        title="Continue",
        control_type="Button"
    )

    continue_btn.click_input()

    print("Waiting for login page...")
    time.sleep(20)

    # =====================================
    # LOGIN TO AXON
    # =====================================
    edge = Desktop(backend="uia").window(
        title_re=".*Sign in.*"
    ).wrapper_object()

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

    # Close any popup that steals focus
    edge.type_keys("{ESC}")

    time.sleep(1)

    edge.set_focus()

    edge.type_keys(otp)

    edge.type_keys("{ENTER}")

    print("Login completed successfully.")
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ensure_login()
