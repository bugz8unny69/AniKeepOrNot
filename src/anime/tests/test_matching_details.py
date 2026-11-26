"""Offline bounds and disclosure checks for per-attempt matching evidence."""
import json
import unittest

from src.anime.history import encode_details
from src.anime.matching import MatchingTrace, from_json, validated


class MatchingDetailsTests(unittest.TestCase):


    def test_diagnostic_bytes_are_not_allowed_for_nonmatching_audits(self):
        with self.assertRaises(ValueError):
            encode_details({"attempt": 1, "unexpected": "secret"})
        self.assertEqual(encode_details({"attempt": 1}), '{"attempt":1}')


if __name__ == "__main__":
    unittest.main()
