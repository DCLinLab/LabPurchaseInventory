"""Direct OpenAI API label reader; never uses Codex or its subscription login."""

import base64
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

from dotenv import dotenv_values
from PIL import Image

from label_reader import INSTRUCTIONS, SCHEMA, ReaderError, input_fingerprint, validate_result
from photo_intake import EXTENSIONS, MAX_IMAGE_BYTES, NoRedirect, image_details


ROOT = Path(__file__).resolve().parent
API_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.6-luna"
MAX_TOTAL_IMAGE_BYTES = 24 * 1024 * 1024


def read_api_settings():
    # Re-read the project file so the local setup dialog can supply a key while
    # the listener stays online. Do not borrow another app's generic API key.
    settings = dotenv_values(ROOT / ".env")
    return {
        "key": settings.get("LABPURCHASE_OPENAI_API_KEY") or os.environ.get("LABPURCHASE_OPENAI_API_KEY", ""),
        "model": settings.get("LABPURCHASE_API_MODEL") or DEFAULT_MODEL,
    }


def image_content(record, folder):
    folder = Path(folder).resolve()
    content, file_ids, total = [], [], 0
    for entry in record["files"]:
        if entry.get("status") != "downloaded":
            continue
        file_id, name = entry["file_id"], entry["local_file"]
        if not re.fullmatch(r"F[A-Za-z0-9]+", file_id) or name not in {file_id + ext for ext in EXTENSIONS.values()}:
            raise ReaderError("invalid_image_path")
        path = (folder / name).resolve()
        if path.parent != folder or not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            raise ReaderError("invalid_image_path")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ReaderError("image_integrity_failed")
        details = image_details(data)
        mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(details["format"])
        with Image.open(BytesIO(data)) as photo:
            if getattr(photo, "n_frames", 1) != 1:
                raise ReaderError("multi_frame_image_needs_conversion")
            if mime is None:
                buffer = BytesIO()
                photo.convert("RGB").save(buffer, format="PNG")
                data, mime = buffer.getvalue(), "image/png"
        total += len(data)
        if len(data) > MAX_IMAGE_BYTES or total > MAX_TOTAL_IMAGE_BYTES:
            raise ReaderError("api_image_batch_too_large")
        content.append({"type": "input_image", "image_url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii"), "detail": "high"})
        file_ids.append(file_id)
    if not file_ids:
        raise ReaderError("no_readable_images")
    return content, file_ids


class APIReader:
    def __init__(self, settings=read_api_settings, opener=None):
        self.settings = settings
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def ready(self):
        return bool(self.settings()["key"].strip())

    def check_ready(self):
        if not self.ready():
            raise ReaderError("bot_api_key_required")

    def read(self, record, folder):
        settings = self.settings()
        key, model = settings["key"].strip(), settings["model"]
        if not key:
            raise ReaderError("bot_api_key_required")
        images, ids = image_content(record, folder)
        payload = {
            "model": model, "instructions": INSTRUCTIONS,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": json.dumps({
                "image_file_ids_in_order": ids, "caption": record["caption"][:12000]}, ensure_ascii=False)}, *images]}],
            "text": {"format": {"type": "json_schema", "name": "package_label", "strict": True, "schema": SCHEMA}},
            "reasoning": {"effort": "none"}, "max_output_tokens": 4096,
            "store": False, "tools": [], "tool_choice": "none",
        }
        request = urllib.request.Request(API_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
                                         headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=120) as response:
                data = response.read(1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            code = {401: "api_authentication_failed", 403: "api_permission_denied", 429: "api_usage_limit"}.get(error.code, "api_request_failed")
            error.close()
            raise ReaderError(code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ReaderError("api_connection_failed") from None
        if len(data) > 1024 * 1024:
            raise ReaderError("api_result_too_large")
        try:
            response = json.loads(data)
            if response.get("status") != "completed":
                raise ReaderError("api_response_incomplete")
            texts = []
            for item in response.get("output", []):
                if item.get("type") == "reasoning":
                    continue
                if item.get("type") != "message":
                    raise ReaderError("unexpected_api_output")
                for part in item.get("content", []):
                    if part.get("type") != "output_text":
                        raise ReaderError("api_response_refused")
                    texts.append(part["text"])
            value = json.loads("".join(texts))
            return {"fields": validate_result(value, ids), "usage": response.get("usage"),
                    "provider": "openai_api", "model": response.get("model", model),
                    "requested_model": model, "response_id": response.get("id"),
                    "input_fingerprint": input_fingerprint(record)}
        except ReaderError:
            raise
        except (ValueError, TypeError, KeyError):
            raise ReaderError("invalid_api_result") from None
