"""Fresh-install authentication invariants (no legacy migration or CI)."""
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest

from src.anime.api.app import Application, PREFIX
from src.anime.api.auth import ApiError, Sessions
from src.anime.api.password_store import PasswordStore
from src.anime.secrets import SecretStore


class FreshAdministrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        os.chmod(self.directory.name, 0o700)
        self.passwords = PasswordStore(self.directory.name)
        self.secrets = SecretStore(self.directory.name)
        self.secrets.initialize(webhook_token="w" * 40)
        self.app = Application.__new__(Application)
        self.app.passwords = self.passwords
        self.app.secrets = self.secrets
        self.app.sessions = Sessions()
        self.app.origin = "https://anime.example.test"
        self.app.logger = logging.getLogger("test.nextgen.password.auth")

    def request(self, method, path, headers=None, body=None):
        return self.app._route(method, PREFIX + path, {'host':'anime.example.test',**(headers or {})},
                               {}, body or {}, "127.0.0.1")

    def test_fresh_schema_has_no_management_token(self):
        record = json.loads((Path(self.directory.name) / "secrets.json").read_text())
        self.assertEqual(record["schema_version"], 8)
        self.assertNotIn("management_token", record["credentials"])
        self.assertTrue(self.secrets.status().webhook_configured)
        self.assertFalse(self.secrets.status().management_configured)

    def test_password_is_required_before_admin_login(self):
        with self.assertRaises(ApiError) as error:
            self.request("POST", "/auth/session",
                         {"origin": self.app.origin}, {"password": "correct horse battery staple"})
        self.assertEqual(error.exception.status, 503)

    def test_password_login_rejects_webhook_and_bearer_bypass(self):
        self.passwords.replace("correct horse battery staple", expected_revision=0)
        with self.assertRaises(ApiError) as error:
            self.request("POST", "/auth/session",
                         {"origin": self.app.origin}, {"token": "w" * 40})
        self.assertEqual(error.exception.status, 400)
        with self.assertRaises(ApiError) as error:
            self.request("GET", "/auth/session",
                         {"authorization": "Bearer " + "w" * 40})
        self.assertEqual(error.exception.status, 401)
        status, result, headers = self.request("POST", "/auth/session",
                         {"origin": self.app.origin}, {"password": "correct horse battery staple"})
        self.assertEqual(status, 201)
        cookie = next(value.decode() for key, value in headers if key == b"set-cookie").split(";", 1)[0]
        status, result, _ = self.request("GET", "/auth/session", {"cookie": cookie})
        self.assertEqual(status, 200)
        self.assertTrue(result["authenticated"])

    def test_recognized_names_requires_admin_session(self):
        self.passwords.replace("correct horse battery staple", expected_revision=0)
        with self.assertRaises(ApiError) as error:
            self.request("GET", "/recognized")
        self.assertEqual(error.exception.status, 401)
        with self.assertRaises(ApiError) as error:
            self.request("GET", "/recognized", {"authorization": "Bearer " + "w"*40})
        self.assertEqual(error.exception.status, 401)

    def test_password_rotation_rejects_old_cookie(self):
        self.passwords.replace("correct horse battery staple", expected_revision=0)
        _, data, headers = self.request("POST", "/auth/session",
            {"origin": self.app.origin}, {"password": "correct horse battery staple"})
        cookie = next(value.decode() for key, value in headers if key == b"set-cookie").split(";", 1)[0]
        status, result, _ = self.request("POST", "/auth/password",
            {"origin": self.app.origin, "cookie": cookie, "x-csrf-token": data["csrf_token"]},
            {"current_password": "correct horse battery staple", "new_password": "different horse battery staple"})
        self.assertEqual(status, 200)
        with self.assertRaises(ApiError) as error:
            self.request("GET", "/auth/session", {"cookie": cookie})
        self.assertEqual(error.exception.status, 401)


if __name__ == "__main__":
    unittest.main()
