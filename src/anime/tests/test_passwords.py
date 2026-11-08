"""Password hash validation and verification."""
import unittest
from src.anime.api.passwords import hash_password, verify_password


class PasswordTests(unittest.TestCase):
    def test_hash_and_verify(self):
        record = hash_password("correct horse battery staple")
        self.assertNotIn("correct horse", record)
        self.assertTrue(verify_password("correct horse battery staple", record))
        self.assertFalse(verify_password("wrong horse battery staple", record))
        self.assertNotEqual(record, hash_password("correct horse battery staple"))

    def test_reject_short_and_malformed(self):
        with self.assertRaises(ValueError):
            hash_password("short")
        self.assertFalse(verify_password("correct horse battery staple", "not-a-hash"))
        self.assertFalse(verify_password("short", hash_password("correct horse battery staple")))


if __name__ == "__main__":
    unittest.main()
