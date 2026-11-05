"""Regression tests for bounded, durable metadata-wait observations."""
import json
import unittest

from src.nextgen.acquisition.metadata_evidence import fresh, validated
from src.nextgen.acquisition.metainfo_source import MetadataSourceWaiting
from src.nextgen.history import encode_details
from src.nextgen.job_contracts import Outcome


class MetadataEvidenceTests(unittest.TestCase):
    def test_metadata_deferral_is_durable_without_changing_retry_policy(self):
        evidence=fresh('poll',state='pausedDL')
        evidence.update(last_active_state='metaDL',stopped_state='pausedDL',
                        resumed=True,polls=24,max_files=0)
        exc=MetadataSourceWaiting(evidence=evidence)
        outcome=Outcome('defer',exc.reason_code,delay_ms=exc.delay_ms,
                        metadata_evidence=exc.evidence)
        saved=json.loads(encode_details({
            'attempt':12, 'metadata_evidence':outcome.metadata_evidence,
        }))
        self.assertEqual(saved['attempt'],12)
        self.assertEqual(saved['metadata_evidence'],evidence)
        self.assertEqual(outcome.kind,'defer')
        self.assertEqual(outcome.delay_ms,60000)

    def test_historical_wait_can_remain_without_metadata_evidence(self):
        outcome=Outcome('defer','TORRENT_METADATA_WAITING',delay_ms=60000)
        self.assertIsNone(outcome.metadata_evidence)

    def test_metadata_evidence_is_restricted_to_metadata_waits(self):
        evidence=fresh('poll')
        with self.assertRaises(ValueError):
            Outcome('defer','JOB_WAITING',delay_ms=60000,metadata_evidence=evidence)
        with self.assertRaises(ValueError):
            Outcome('retry','TORRENT_METADATA_WAITING',metadata_evidence=evidence)

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
