import os
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from google.oauth2.credentials import Credentials

from google_auth import EXPECTED_EMAIL, GoogleAuthError, SCOPES, protect, save_credentials, verify_account


class GoogleAuthTests(unittest.TestCase):
    def session(self, email):
        factory = Mock()
        session = factory.return_value.__enter__ = Mock(return_value=Mock())
        factory.return_value.__exit__ = Mock(return_value=False)
        session.return_value.get.return_value.status_code = 200
        session.return_value.get.return_value.json.return_value = {"emailAddress": email}
        return factory

    def test_wrong_google_account_is_rejected(self):
        credentials = Credentials(token="test", scopes=SCOPES)
        with self.assertRaisesRegex(GoogleAuthError, "wrong_google_account"):
            verify_account(credentials, self.session("someone-else@gmail.com"))
        self.assertEqual(verify_account(credentials, self.session(EXPECTED_EMAIL)), EXPECTED_EMAIL)

    def test_partially_granted_consent_is_rejected_before_profile_read(self):
        factory = Mock()
        credentials = Credentials(token="test", scopes=SCOPES, granted_scopes=SCOPES[:1])
        with self.assertRaisesRegex(GoogleAuthError, "permissions_incomplete"):
            verify_account(credentials, factory)
        factory.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI storage")
    def test_credentials_encryption_roundtrip_and_tamper_rejection(self):
        plaintext = b"synthetic-refresh-token-for-storage-test"
        encrypted = protect(plaintext)
        self.assertNotIn(plaintext, encrypted)
        self.assertEqual(protect(encrypted, decrypt=True), plaintext)
        with self.assertRaises(GoogleAuthError):
            protect(encrypted[:-1], decrypt=True)

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI storage')
    def test_concurrent_worker_saves_are_atomic_and_leave_no_temp_files(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'token.dpapi'
            def save(index):
                credential = Mock()
                credential.to_json.return_value = json.dumps({'synthetic': index})
                save_credentials(credential, path)
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(save, range(24)))
            data = json.loads(protect(path.read_bytes(), decrypt=True))
            self.assertIn(data['synthetic'], range(24))
            self.assertEqual(list(Path(temp).glob('*.tmp')), [])


if __name__ == "__main__":
    unittest.main()
