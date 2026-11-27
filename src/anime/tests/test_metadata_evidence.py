"""Regression tests for bounded, durable metadata-wait observations."""
import json
import unittest

from src.anime.acquisition.metadata_evidence import fresh, validated
from src.anime.history import encode_details


class MetadataEvidenceTests(unittest.TestCase):



    def test_metadata_evidence_must_be_bounded_and_redacted(self):
        for key,value in (
            ('phase','torrent_secret'),
            ('last_active_state','metaDL\nsecret'),
            ('polls',25),
            ('max_files',10001),
            ('resumed','true'),
        ):
            with self.subTest(key=key):
                evidence=fresh('poll')
                evidence[key]=value
                with self.assertRaises(ValueError):
                    validated(evidence)
                with self.assertRaises(ValueError):
                    encode_details({'attempt':1,'metadata_evidence':evidence})
        invalid=fresh('poll')
        invalid['tracker_url']='http://private.example/announce'
        with self.assertRaises(ValueError):
            validated(invalid)


if __name__ == '__main__':
    unittest.main()
