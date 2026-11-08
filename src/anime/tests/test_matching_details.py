"""Offline bounds and disclosure checks for per-attempt matching evidence."""
import json
import unittest

from src.anime.history import encode_details
from src.anime.job_contracts import Outcome
from src.anime.matching import MatchingTrace, from_json, validated


class MatchingDetailsTests(unittest.TestCase):
    def test_sequel_evidence_is_bounded_and_not_an_identity_override(self):
        trace = MatchingTrace()
        trace.queries(["ひぐらしのなく頃に" * 30], season=2)
        trace.check("release_title", "passed")
        trace.check("season_scope", "failed", "unverified_sequel")
        raw = trace.dump()
        self.assertEqual(from_json(raw)["season"], 2)
        outcome = Outcome("review", "IDENTITY_INCOMPLETE", matching=raw)
        self.assertEqual(outcome.matching, raw)
        stored = encode_details({"attempt": 1, "matching": trace.data})
        self.assertEqual(json.loads(stored)["matching"]["checks"][-1]["note"],
                         "unverified_sequel")
        self.assertLessEqual(len(stored.encode("utf-8")), 9216)

    def test_match_details_refuse_tokens_arbitrary_responses_and_unknown_steps(self):
        trace = MatchingTrace()
        trace.check("release_title", "passed")
        trace.queries(["Anime title"], season=1)
        evidence = trace.data
        for changed in (
            {**evidence, "bearer_token": "secret"},
            {**evidence, "checks": [{"step": "arbitrary_provider_url",
                                   "status": "passed", "note": None}]},
            {**evidence, "checks": [{"step": "release_title", "status": "passed",
                                   "note": "https://private.example/token=secret"}]},
            {**evidence, "queries": ["A" * 10000]},
            {**evidence, "anidb_candidates": [{"anidb_id": -1, "titles": ["bad"]}]},
        ):
            with self.assertRaises((ValueError, TypeError)):
                validated(changed)
        with self.assertRaises((ValueError, TypeError)):
            Outcome("review", "IDENTITY_INCOMPLETE", matching='{"token":"secret"}')
        with self.assertRaises(ValueError):
            encode_details({"attempt": 1, "matching": {**evidence, "access_key": "private"}})

    def test_diagnostic_bytes_are_not_allowed_for_nonmatching_audits(self):
        with self.assertRaises(ValueError):
            encode_details({"attempt": 1, "unexpected": "secret"})
        self.assertEqual(encode_details({"attempt": 1}), '{"attempt":1}')


if __name__ == "__main__":
    unittest.main()
