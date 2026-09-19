"""Desktop Google authorization for the lab account; no mailbox or sheet writes."""

import argparse
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import tempfile
from threading import RLock
import time

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).resolve().parent
CLIENT_PATH = ROOT / ".local" / "google-client.json"
TOKEN_PATH = ROOT / ".local" / "google-token.dpapi"
EXPECTED_EMAIL = "linjhumse@gmail.com"
_CREDENTIAL_LOCK = RLock()
# drive.file only covers files created by or explicitly opened with this app.
# It does not give access to every file in the account.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/drive.file"]


class GoogleAuthError(ValueError):
    """Fixed, non-secret diagnostics suitable for display."""


def protect(data, decrypt=False):
    """Encrypt credentials for the current Windows user using Windows DPAPI."""
    if os.name != "nt":
        raise GoogleAuthError("windows_credential_storage_required")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise GoogleAuthError("google_credential_storage_failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def verify_account(credentials, session_factory=AuthorizedSession):
    granted = credentials.granted_scopes
    if isinstance(granted, str):
        granted = granted.split()
    if not credentials.has_scopes(SCOPES) or (granted is not None and not set(SCOPES).issubset(granted)):
        raise GoogleAuthError("google_permissions_incomplete")
    with session_factory(credentials) as session:
        # Profile only. No message bodies, subjects, or spreadsheet data are read.
        response = session.get("https://gmail.googleapis.com/gmail/v1/users/me/profile", timeout=30)
        if response.status_code != 200:
            raise GoogleAuthError("google_profile_check_failed_enable_gmail_api")
        address = response.json().get("emailAddress", "").strip().lower()
    if address != EXPECTED_EMAIL:
        raise GoogleAuthError("wrong_google_account_not_saved")
    return address


def save_credentials(credentials, path=TOKEN_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = protect(credentials.to_json().encode("utf-8"))
    # Email and receipt workers may start together. Never share a temporary file;
    # serialize in-process saves and tolerate a brief Windows reader lock.
    with _CREDENTIAL_LOCK:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encrypted)
        try:
            for attempt in range(10):
                try:
                    temporary.replace(path)
                    break
                except PermissionError:
                    if attempt == 9:
                        raise GoogleAuthError('google_credential_storage_busy')
                    time.sleep(0.05)
        finally:
            temporary.unlink(missing_ok=True)


def load_credentials(path=TOKEN_PATH):
    path = Path(path)
    if not path.is_file():
        raise GoogleAuthError("google_authorization_required")
    data = json.loads(protect(path.read_bytes(), decrypt=True).decode("utf-8"))
    # Check the stored scopes rather than replacing them with requested scopes.
    credentials = Credentials.from_authorized_user_info(data)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    verify_account(credentials)
    save_credentials(credentials, path)
    return credentials


def connect(open_browser=True):
    # The library's callback logger may include the authorization code in a URL.
    logging.getLogger("google_auth_oauthlib.flow").disabled = True
    if not CLIENT_PATH.is_file():
        raise GoogleAuthError("google_desktop_client_required_in_local_google_client_json")
    config = json.loads(CLIENT_PATH.read_text(encoding="utf-8"))
    installed = config.get("installed", {})
    # Only use Google's endpoints from a Google Desktop OAuth client download.
    if (installed.get("auth_uri") != "https://accounts.google.com/o/oauth2/auth"
            or installed.get("token_uri") != "https://oauth2.googleapis.com/token"):
        raise GoogleAuthError("invalid_google_desktop_client")
    flow = InstalledAppFlow.from_client_config(config, SCOPES, autogenerate_code_verifier=True)
    credentials = flow.run_local_server(
        host="127.0.0.1", port=0, open_browser=open_browser, timeout_seconds=600,
        prompt="consent select_account", login_hint=EXPECTED_EMAIL,
        authorization_prompt_message="Open this Google authorization page:\n{url}",
        success_message="Google returned authorization. You can close this tab; the bot will verify the lab account before saving it.")
    verify_account(credentials)
    if not credentials.refresh_token:
        raise GoogleAuthError("google_offline_access_missing")
    save_credentials(credentials)
    return credentials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify saved login, without reading email or sheets.")
    parser.add_argument("--no-browser", action="store_true", help="Print the consent URL instead of opening the default browser.")
    args = parser.parse_args()
    try:
        if args.check:
            load_credentials()
        else:
            connect(open_browser=not args.no_browser)
        print(f"Google authorization verified for {EXPECTED_EMAIL}; credentials encrypted for this Windows user.")
        return 0
    except GoogleAuthError as error:
        print(f"Google setup: {error}")
    except Exception:
        # OAuth exceptions can contain authorization codes, URLs, and tokens.
        print("Google authorization did not complete. Check consent, enabled APIs and connection, then retry.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
