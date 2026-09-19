"""Download Slack photos into a private, restart-safe queue for label extraction."""

import hashlib
import io
import json
import re
import threading
import warnings
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image
from slack_sdk.errors import SlackApiError


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_FILES_PER_MESSAGE = 20
EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif", "TIFF": ".tif"}


class PhotoError(ValueError):
    """A safe error code that can be logged without disclosing credentials."""


def validate_download_url(url):
    try:
        parsed = urlsplit(url)
        allowed = (
            parsed.scheme == "https" and parsed.hostname == "files.slack.com"
            and parsed.port in (None, 443) and not parsed.username and not parsed.password
        )
    except (TypeError, ValueError):
        allowed = False
    if not allowed:
        raise PhotoError("untrusted_download_url")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward Slack credentials to a redirect destination.
        return None


def download_photo(url, token, *, opener=None, max_bytes=MAX_IMAGE_BYTES):
    validate_download_url(url)
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    opener = opener or build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise PhotoError("image_too_large")
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise PhotoError("image_too_large")
            if not data:
                raise PhotoError("empty_image")
            return data
    except HTTPError as error:
        raise PhotoError(f"download_http_{error.code}") from None
    except (URLError, TimeoutError, OSError):
        raise PhotoError("download_unavailable") from None
    except ValueError as error:
        if isinstance(error, PhotoError):
            raise
        raise PhotoError("invalid_content_length") from None


def image_details(data):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise PhotoError("image_dimensions_too_large")
                if image.format not in EXTENSIONS:
                    raise PhotoError("unsupported_image_format")
                result = {"format": image.format, "width": image.width, "height": image.height}
                image.verify()
            # Decode as well: JPEG verify() alone does not detect every truncated file.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
        return result
    except PhotoError:
        raise
    except Exception:
        raise PhotoError("invalid_or_unsupported_image") from None


def write_json(path, value):
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


class PhotoIntake:
    def __init__(self, root, downloader=download_photo):
        self.root = Path(root)
        self.downloader = downloader
        self._lock = threading.Lock()

    def capture(self, event, client, token):
        """Called only after the receiver checks the workspace, app and channel."""
        files = event.get("files") or []
        if not files:
            return None
        if len(files) > MAX_FILES_PER_MESSAGE:
            raise PhotoError("too_many_files_in_message")
        channel, ts = event.get("channel", ""), event.get("ts", "")
        if not re.fullmatch(r"C[A-Za-z0-9]+", channel) or not re.fullmatch(r"\d+\.\d+", ts):
            raise PhotoError("invalid_message_identity")
        with self._lock:
            return self._capture(event, files, client, token)

    def _capture(self, event, files, client, token):
        folder = self.root / f"{event['channel']}_{event['ts']}"
        manifest = folder / "record.json"
        record = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {
            "schema_version": 1,
            "channel_id": event["channel"], "message_ts": event["ts"],
            "thread_ts": event.get("thread_ts") or event["ts"],
            "user_id": event["user"], "caption": event.get("text", ""),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "status": "capturing", "files": [],
            "extracted_fields": None, "inventory_change_applied": False,
        }
        entries = {item["file_id"]: item for item in record["files"]}
        for attachment in files:
            file_id = attachment.get("id", "")
            if not re.fullmatch(r"F[A-Za-z0-9]+", file_id):
                raise PhotoError("invalid_file_identity")
            existing = entries.get(file_id)
            if existing and existing.get("status") == "downloaded":
                # Use a file-ID-derived basename, never a sender-provided path.
                cached_name = existing.get("local_file", "")
                valid_name = any(cached_name == file_id + ext for ext in EXTENSIONS.values())
                if valid_name:
                    cached = folder / cached_name
                    if cached.is_file() and hashlib.sha256(cached.read_bytes()).hexdigest() == existing.get("sha256"):
                        continue
            try:
                metadata = client.files_info(file=file_id)["file"]
                if metadata.get("id") != file_id:
                    raise PhotoError("file_identity_mismatch")
                if not metadata.get("mimetype", "").startswith("image/"):
                    continue
                if metadata.get("is_external"):
                    raise PhotoError("external_images_not_supported")
                if int(metadata.get("size") or 0) > MAX_IMAGE_BYTES:
                    raise PhotoError("image_too_large")
                url = metadata.get("url_private_download") or metadata.get("url_private")
                if not url:
                    raise PhotoError("photo_download_not_available")
                data = self.downloader(url, token)
                if len(data) > MAX_IMAGE_BYTES:
                    raise PhotoError("image_too_large")
                details = image_details(data)
                filename = file_id + EXTENSIONS[details["format"]]
                folder.mkdir(parents=True, exist_ok=True)
                temp = folder / (filename + ".part")
                temp.write_bytes(data)
                temp.replace(folder / filename)
                entries[file_id] = {
                    "file_id": file_id, "name": metadata.get("name"),
                    "mimetype": metadata.get("mimetype"), "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(), "local_file": filename,
                    "status": "downloaded", **details,
                }
            except SlackApiError as error:
                entries[file_id] = {"file_id": file_id, "status": "failed", "error": "slack_" + str(error.response.get("error", "unknown"))}
            except PhotoError as error:
                entries[file_id] = {"file_id": file_id, "status": "failed", "error": str(error)}
            record["files"] = list(entries.values())
            folder.mkdir(parents=True, exist_ok=True)
            write_json(manifest, record)
        if not entries:
            return None
        downloaded = sum(item["status"] == "downloaded" for item in entries.values())
        failed = sum(item["status"] == "failed" for item in entries.values())
        record["status"] = "awaiting_ai" if downloaded and not failed else "partial" if downloaded else "download_failed"
        record["files"] = list(entries.values())
        write_json(manifest, record)
        return record
