import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

from PIL import Image

from photo_intake import PhotoError, PhotoIntake, download_photo, image_details


def png_bytes():
    output = io.BytesIO()
    Image.new("RGB", (12, 8), "white").save(output, format="PNG")
    return output.getvalue()


class DownloadTests(unittest.TestCase):
    def test_credentials_are_not_sent_to_other_hosts(self):
        opener = Mock()
        for url in ("https://attacker.invalid/a", "http://files.slack.com/a", "https://files.slack.com.attacker.invalid/a", "https://user@files.slack.com/a", "https://files.slack.com:444/a", "file:///secret", None):
            with self.subTest(url=url), self.assertRaises(PhotoError):
                download_photo(url, "secret", opener=opener)
        opener.open.assert_not_called()

    def test_declared_and_streamed_sizes_are_bounded(self):
        for declared in ("1000", None):
            response = Mock()
            response.headers = {"Content-Length": declared}
            response.read.return_value = b"X" * 11
            context = Mock()
            context.__enter__ = Mock(return_value=response)
            context.__exit__ = Mock(return_value=False)
            opener = Mock()
            opener.open.return_value = context
            with self.assertRaisesRegex(PhotoError, "image_too_large"):
                download_photo("https://files.slack.com/a", "secret", opener=opener, max_bytes=10)

    def test_redirect_is_reported_without_exposing_url_or_credentials(self):
        opener = Mock()
        opener.open.side_effect = HTTPError("https://files.slack.com/a?secret=1", 302, "redirect", {}, None)
        with self.assertRaisesRegex(PhotoError, "^download_http_302$"):
            download_photo("https://files.slack.com/a", "secret", opener=opener)

    def test_html_login_page_is_not_saved_as_a_photo(self):
        with self.assertRaises(PhotoError):
            image_details(b"<html>Please sign in</html>")


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = png_bytes()
        self.downloader = Mock(return_value=self.data)
        self.client = Mock()
        self.client.files_info.return_value = {"file": {
            "id": "F123", "name": "../../untrusted-name.png", "mimetype": "image/png",
            "url_private": "https://files.slack.com/files-pri/test.png", "size": len(self.data),
        }}
        self.event = {"channel": "Ctarget", "ts": "100.1", "user": "Uhuman", "text": "Put it in 365", "files": [{"id": "F123"}]}
        self.intake = PhotoIntake(self.root, self.downloader)

    def test_caption_and_original_bytes_preserved_without_assuming_inventory(self):
        record = self.intake.capture(self.event, self.client, "xoxb-private")
        self.assertEqual(record["status"], "awaiting_ai")
        self.assertEqual(record["caption"], "Put it in 365")
        self.assertIsNone(record["extracted_fields"])
        self.assertFalse(record["inventory_change_applied"])
        image = self.root / "Ctarget_100.1" / "F123.png"
        self.assertEqual(image.read_bytes(), self.data)
        self.assertEqual(record["files"][0]["sha256"], hashlib.sha256(self.data).hexdigest())
        saved = (image.parent / "record.json").read_text()
        self.assertNotIn("url_private", saved)
        self.assertNotIn("xoxb-private", saved)

    def test_restart_reuses_verified_download(self):
        self.intake.capture(self.event, self.client, "token")
        PhotoIntake(self.root, self.downloader).capture(self.event, self.client, "token")
        self.downloader.assert_called_once()
        self.client.files_info.assert_called_once()

    def test_corrupt_cached_photo_is_downloaded_again(self):
        self.intake.capture(self.event, self.client, "token")
        (self.root / "Ctarget_100.1" / "F123.png").write_bytes(b"corrupted")
        self.intake.capture(self.event, self.client, "token")
        self.assertEqual(self.downloader.call_count, 2)

    def test_thread_identity_is_preserved(self):
        self.event["thread_ts"] = "99.1"
        record = self.intake.capture(self.event, self.client, "token")
        self.assertEqual(record["thread_ts"], "99.1")

    def test_bad_photo_can_be_retried_without_a_false_success(self):
        self.downloader.return_value = b"not an image"
        record = self.intake.capture(self.event, self.client, "token")
        self.assertEqual(record["status"], "download_failed")
        self.downloader.return_value = self.data
        record = self.intake.capture(self.event, self.client, "token")
        self.assertEqual(record["status"], "awaiting_ai")

    def test_non_image_attachment_is_skipped(self):
        self.client.files_info.return_value["file"]["mimetype"] = "application/pdf"
        self.assertIsNone(self.intake.capture(self.event, self.client, "token"))
        self.downloader.assert_not_called()

    def test_untrusted_message_and_file_paths_are_rejected(self):
        for field, value in (("channel", "../outside"), ("ts", "../../outside"), ("files", [{"id": "../../outside"}])):
            event = {**self.event, field: value}
            with self.assertRaises(PhotoError):
                self.intake.capture(event, self.client, "token")


if __name__ == "__main__":
    unittest.main()
