"""Password storage CAS and security regression tests."""
import os
from pathlib import Path
import tempfile
import unittest

from src.anime.api.password_store import PasswordStore, PasswordConflict, PasswordStorageError


class PasswordStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.chmod(self.tmp.name, 0o700)
        self.store = PasswordStore(self.tmp.name)

    def test_create_rotate_and_compare_and_swap(self):
        self.assertEqual(self.store.read(), (0, None))
        self.assertEqual(self.store.replace("correct horse battery staple", expected_revision=0), 1)
        self.assertEqual(self.store.verify("correct horse battery staple"), (1, True))
        with self.assertRaises(PasswordConflict):
            self.store.replace("new correct horse battery staple", expected_revision=0)
        self.assertEqual(self.store.replace("new correct horse battery staple", expected_revision=1), 2)
        self.assertEqual(self.store.verify("correct horse battery staple"), (2, False))
        self.assertEqual(self.store.verify("new correct horse battery staple"), (2, True))
        self.assertEqual(os.stat(Path(self.tmp.name) / "management-password.json").st_mode & 0o777, 0o600)

    def test_reject_symlink(self):
        (Path(self.tmp.name) / "management-password.json").symlink_to("/etc/passwd")
        with self.assertRaises(PasswordStorageError):
            self.store.read()

    def test_reject_insecure_directory(self):
        os.chmod(self.tmp.name, 0o777)
        with self.assertRaises(PasswordStorageError):
            self.store.read()


if __name__ == "__main__":
    unittest.main()
