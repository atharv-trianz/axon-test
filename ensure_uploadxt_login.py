
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
AGENCY_URL = "ingest-tbi-test-portal.evidence.com"

UPLOAD_XT_PATH = (
    r"C:\Program Files\Axon Enterprise\Axon Evidence Upload XT v2"
    r"\CLIENT\Axon Evidence Upload XT v2.exe"
)

_secrets_client = boto3.client("secretsmanager", region_name=RESOURCE_REGION)
_cached_creds = None


def _get_axon_credentials() -> dict:
    global _cached_creds
    if _cached_creds is not None:
        return _cached_creds

    resp = _secrets_client.get_secret_value(SecretId=AXON_CREDS_SECRET_ARN)
    creds = json.loads(resp["SecretString"])

    missing = [k for k in ("username", "password", "totpSecret") if not creds.get(k)]
    if missing:
        raise RuntimeError(f"Secrets Manager secret missing: {', '.join(missing)}")

    _cached_creds = creds
    return creds


def ensure_login() -> bool:
    creds = _get_axon_credentials()
    USERNAME = creds["username"]
    PASSWORD = creds["password"]
    SECRET = creds["totpSecret"]

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
    edge.type_keys(USERNAME)
    edge.type_keys("{TAB}")

    # Password
    edge.type_keys("^a{DEL}")
    edge.type_keys(PASSWORD)
    edge.type_keys("{ENTER}")

    # =====================================
    # MFA OTP
    # =====================================
    time.sleep(3)

    otp = pyotp.TOTP(SECRET).now()

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
