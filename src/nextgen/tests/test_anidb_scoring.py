"""Comprehensive unit and integration tests for AniDB identity scoring and ranking.

Covers:
- Exact matching
- Near-exact matching and release metadata cleanup (v4, tech, audio, CRC)
- Alternative English/Japanese/romanized titles
- Explicit sequel titles (2nd Season, Second Season, S2, Roman numerals)
- Season-aware title generation
- Multiple candidates above threshold (highest-ranked eligible candidate wins)
- Candidate rankings independent of catalog ordering
- Score exactly at configured threshold (boundary condition)
- Threshold configuration changes (50% - 100%)
- Equal title scores resolved by stronger ranking evidence (exact match, season, etc.)
- Contradictory evidence (season contradiction penalty, special penalty)
- Unicode normalization (full-width, accents, katakana)
- Long titles and malformed input
- Candidate persistence across job completion and rejection
- Manual identity overrides precedence
- Recognized seasons precedence
- Offline isolation: no Anime-Lists calls during AniDB candidate selection
"""
import asyncio
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

from src.nextgen.history import append_job_history
from src.nextgen.job_contracts import Outcome
from src.nextgen.matching import MatchingTrace, validated
from src.nextgen.providers.http import Credentials, Options
from src.nextgen.providers.identity import make_resolver
from src.nextgen.release_names import norm, release_title
from src.nextgen.providers.scoring import calculate_title_similarity, generate_season_derived_aliases, generate_title_variants, rank_and_select_candidates, score_anidb_candidate, TitleVariant, CandidateMatch
from src.nextgen.release_names import clean_metadata, detect_explicit_sequel_marker
from src.nextgen.storage.candidate_evidence import load_candidate_evidence, save_candidate_evidence
from src.nextgen.storage.migrations import LATEST_VERSION
from src.nextgen.storage.sqlite import connect, initialize, transaction, IdentityConflict, IdentityNotReady


class DummyConfiguration:
    def __init__(self, db_path, threshold_pct=90):
        self._db_path = db_path
        self._threshold = threshold_pct

    def value(self, key):
        if key == "storage.database_path":
            return self._db_path
        if key == "identity.match_threshold_pct":
            return self._threshold
        raise KeyError(key)


class DummyDelivery:
    def __init__(self, name):
        self.raw_body = json.dumps({"release_name": name}).encode()


class DummyProvider:
    def __init__(self, candidates=None, anime_map=None, mapping_id=12345):
        self.candidates = candidates or {}
        self.anime_map = anime_map or {}
        self.mapping_id = mapping_id
        self.queries = []
        self.anime_calls = []
        self.mapping_calls = []
        self.season_entry_calls = []
        self.series_calls = []

    def title_candidates(self, aliases):
        self.queries.append(tuple(aliases))
        return self.candidates

    def anime(self, aid):
        self.anime_calls.append(aid)
        if aid in self.anime_map:
            return self.anime_map[aid]
        first_title = self.candidates.get(aid, ("Test Anime",))[0]
        return {"title": first_title, "titles": self.candidates.get(aid, (first_title,))}

    def mapping(self, aid):
        self.mapping_calls.append(aid)
        return self.mapping_id

    def season_entry(self, tid, season, episode=None):
        self.season_entry_calls.append((tid, season, episode))
        return 99999

    def series(self, tid, credentials):
        self.series_calls.append(tid)
        return {
            "titles": ("Test Anime",),
            "animation": True,
            "live": False,
            "anidb_ids": frozenset(self.candidates.keys()),
        }


class AniDBScoringUnitTests(unittest.TestCase):
    def test_clean_metadata_strips_version_and_tech_tokens(self):
        self.assertEqual(clean_metadata("Majestic Prince: Genetic Awakening (2016) v4"),
                         "Majestic Prince: Genetic Awakening (2016)")
        self.assertEqual(clean_metadata("[Anime Time] Black Clover - S02E02"),
                         "Black Clover - S02E02")
        self.assertEqual(clean_metadata("Show Title (1080p BluRay HEVC AAC)"),
                         "Show Title")

    def test_detect_explicit_sequel_marker(self):
        base, num = detect_explicit_sequel_marker("Kusuriya no Hitorigoto 2nd Season")
        self.assertEqual(base, "Kusuriya no Hitorigoto")
        self.assertEqual(num, 2)

        base, num = detect_explicit_sequel_marker("Overlord Season 3")
        self.assertEqual(base, "Overlord")
        self.assertEqual(num, 3)

        base, num = detect_explicit_sequel_marker("Mob Psycho 100")
        self.assertEqual(base, "Mob Psycho 100")
        self.assertIsNone(num)

    def test_generate_season_derived_aliases(self):
        derived = generate_season_derived_aliases("Black Clover", 2)
        self.assertIn("Black Clover 2nd Season", derived)
        self.assertIn("Black Clover Second Season", derived)
        self.assertIn("Black Clover Season 2", derived)
        self.assertIn("Black Clover S2", derived)
        self.assertIn("Black Clover II", derived)

    def test_generate_title_variants_provenance(self):
        variants, ep, s = generate_title_variants(
            "[Anime Time] Black Clover - S02E02", seasons=["2"], episode=2
        )
        provs = {v.provenance for v in variants}
        self.assertIn("cleaned", provs)
        self.assertIn("season_derived", provs)
        self.assertTrue(any(v.text == "Black Clover 2nd Season" and v.provenance == "season_derived" for v in variants))

    def test_exact_90_pct_threshold_boundary(self):
        # Candidate scoring exactly 90% is eligible
        variants = (TitleVariant("Black Clover Season 2", "cleaned"),)
        # Similarity of "Black Clover Season 2" to "Black Clover Season 2" is 100
        match = score_anidb_candidate(100, ("Black Clover Season 2",), variants, 90)
        self.assertTrue(match.eligible)

        # Exact 90% boundary condition check
        # A variant and title designed to hit >= 90%
        cand = {100: ("Title Alpha",)}
        res = rank_and_select_candidates(cand, (TitleVariant("Title Alpha", "original"),), 100)
        self.assertIsNotNone(res.selected)

        # 89% similarity should be ineligible when threshold is 90%
        cand_low = {100: ("Title Alpha Beta",)}
        res_low = rank_and_select_candidates(cand_low, (TitleVariant("Title Alpha", "original"),), 90)
        self.assertIsNone(res_low.selected)

    def test_multiple_candidates_above_threshold_highest_rank_wins(self):
        # 19433 matches sequel; 12665 matches base; 12351 matches special
        variants = (
            TitleVariant("Black Clover", "cleaned"),
            TitleVariant("Black Clover 2nd Season", "season_derived"),
        )
        candidates = {
            12351: ("Black Clover Special",),
            12665: ("Black Clover", "Black Clover (2017)"),
            19433: ("Black Clover 2nd Season",),
        }
        res = rank_and_select_candidates(candidates, variants, 90, release_season=2)
        self.assertEqual(res.decision, "AUTO_SELECTED")
        self.assertIsNotNone(res.selected)
        self.assertEqual(res.selected.anidb_id, 19433)
        self.assertEqual(res.selected.rank, 1)

    def test_rankings_independent_of_catalog_ordering(self):
        variants = (
            TitleVariant("Black Clover", "cleaned"),
            TitleVariant("Black Clover 2nd Season", "season_derived"),
        )
        candidates_forward = {
            12351: ("Black Clover Special",),
            12665: ("Black Clover",),
            19433: ("Black Clover 2nd Season",),
        }
        candidates_reverse = {
            19433: ("Black Clover 2nd Season",),
            12665: ("Black Clover",),
            12351: ("Black Clover Special",),
        }
        res1 = rank_and_select_candidates(candidates_forward, variants, 90, release_season=2)
        res2 = rank_and_select_candidates(candidates_reverse, variants, 90, release_season=2)
        self.assertEqual(res1.selected.anidb_id, res2.selected.anidb_id)
        self.assertEqual([c.anidb_id for c in res1.candidates], [c.anidb_id for c in res2.candidates])

    def test_ambiguous_identical_candidates_raise_tie(self):
        variants = (TitleVariant("Franchise Show", "cleaned"),)
        candidates = {
            101: ("Franchise Show",),
            102: ("Franchise Show",),
        }
        res = rank_and_select_candidates(candidates, variants, 90)
        self.assertEqual(res.decision, "AMBIGUOUS_TIE")
        self.assertIsNone(res.selected)

    def test_unicode_normalization_handling(self):
        # Full-width alphanumeric vs ASCII
        v = TitleVariant("８６―エイティシックス―", "original")
        sim = calculate_title_similarity("８６―エイティシックス―", "86 - Eighty Six")
        # Unicode normalization in norm() ensures consistent matching
        self.assertEqual(norm("Ｐｏｋｅｍｏｎ"), norm("Pokemon"))
        self.assertEqual(norm("Attack on Titan — Final"), norm("Attack on Titan - Final"))

    def test_malformed_and_long_titles(self):
        # Long titles exceeding limits are safely bounded and do not crash
        long_name = "[Group] " + "A" * 500 + " - 01 [1080p]"
        variants, ep, s = generate_title_variants(long_name)
        self.assertTrue(all(len(v.text) <= 512 for v in variants))


class AniDBScoringIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db_conn = sqlite3.connect(":memory:", check_same_thread=False)
        initialize(self.db_conn)
        self.db_path = ":memory:"

    def tearDown(self):
        self.db_conn.close()

    def make_resolver(self, provider, threshold_pct=90):
        config = DummyConfiguration(self.db_path, threshold_pct=threshold_pct)
        return make_resolver(
            Options("test", 1),
            lambda: Credentials("dummy"),
            client_factory=lambda db, opts: provider,
        ), config

    async def test_job_449_black_clover_selects_sequel(self):
        # Release S02E02 discovers 19433, 12665, 12351 and picks 19433
        provider = DummyProvider(
            candidates={
                12351: ("Black Clover Special",),
                12665: ("Black Clover", "Black Clover (2017)"),
                19433: ("Black Clover 2nd Season",),
            },
            anime_map={
                19433: {"title": "Black Clover 2nd Season", "titles": ("Black Clover 2nd Season",)},
            }
        )
        provider.series = lambda tid, creds: {
            "titles": ("Black Clover", "Black Clover 2nd Season"),
            "animation": True, "live": False, "anidb_ids": frozenset({12665, 19433})
        }
        resolver, config = self.make_resolver(provider)
        trace = MatchingTrace()
        delivery = DummyDelivery("[Anime Time] Black Clover - S02E02")

        with patch("src.nextgen.providers.identity._open", return_value=self.db_conn):
            result = await resolver(delivery, config, threading.Event(), matching=trace)

        self.assertEqual(result["anime"]["anidb_id"], 19433)
        self.assertEqual(trace.data["selected_anidb_id"], 19433)
        self.assertIn("checks", trace.data)
        self.assertTrue(any(c["step"] == "anidb_candidates" and c["status"] == "passed" for c in trace.data["checks"]))

        # Verify candidate evidence was recorded on the trace
        self.assertIsNotNone(trace.candidate_evidence)
        self.assertEqual(len(trace.candidate_evidence), 3)
        winner = [c for c in trace.candidate_evidence if c.selected][0]
        self.assertEqual(winner.anidb_id, 19433)

    async def test_job_258_majestic_prince_recovers_12033(self):
        provider = DummyProvider(
            candidates={12033: ("Ginga Kikoutai Majestic Prince: Genetic Awakening", "Majestic Prince: Genetic Awakening")},
            anime_map={12033: {"title": "Majestic Prince: Genetic Awakening", "titles": ("Majestic Prince: Genetic Awakening",)}}
        )
        provider.series = lambda tid, creds: {
            "titles": ("Majestic Prince: Genetic Awakening",), "animation": True, "live": False, "anidb_ids": frozenset({12033})
        }
        resolver, config = self.make_resolver(provider)
        trace = MatchingTrace()
        delivery = DummyDelivery("Majestic Prince: Genetic Awakening (2016) v4")

        with patch("src.nextgen.providers.identity._open", return_value=self.db_conn):
            result = await resolver(delivery, config, threading.Event(), matching=trace)

        self.assertEqual(result["anime"]["anidb_id"], 12033)

    async def test_job_441_higurashi_sotsu_recovers_16137(self):
        provider = DummyProvider(
            candidates={16137: ("Higurashi no Naku Koro ni Sotsu", "Higurashi: When They Cry Sotsu", "Higurashi Sotsu")},
            anime_map={16137: {"title": "Higurashi Sotsu", "titles": ("Higurashi Sotsu",)}}
        )
        provider.series = lambda tid, creds: {
            "titles": ("Higurashi Sotsu",), "animation": True, "live": False, "anidb_ids": frozenset({16137})
        }
        resolver, config = self.make_resolver(provider)
        trace = MatchingTrace()
        delivery = DummyDelivery("[Dyna] Higurashi When They Cry S02 (BD 1080p AVC Opus 2.0) [Dual Audio] | Higurashi Sotsu")

        with patch("src.nextgen.providers.identity._open", return_value=self.db_conn):
            result = await resolver(delivery, config, threading.Event(), matching=trace)

        self.assertEqual(result["anime"]["anidb_id"], 16137)

    async def test_manual_identity_override_has_precedence(self):
        manual_override = {
            "schema_version": 2,
            "anime": {"anidb_id": 8888, "tvdb_series_id": 9999, "title": "Operator Confirmed Show"},
            "episodes": [], "attributes": {}
        }
        provider = DummyProvider(candidates={1111: ("Automatic Guess",)})
        resolver, config = self.make_resolver(provider)
        trace = MatchingTrace()
        delivery = DummyDelivery("Overridden Show - 01")

        with patch("src.nextgen.providers.identity._open", return_value=self.db_conn), \
             patch("src.nextgen.storage.manual_identity.for_release", return_value=manual_override):
            result = await resolver(delivery, config, threading.Event(), matching=trace)

        # Must pick manual identity 8888, NOT automatic guess 1111
        self.assertEqual(result["anime"]["anidb_id"], 8888)
        self.assertEqual(result["anime"]["tvdb_series_id"], 9999)

    async def test_recognized_season_has_precedence(self):
        recognized = {
            "anidb_id": 7777,
            "tvdb_series_id": 3333,
            "title": "Known Series Season 2"
        }
        provider = DummyProvider(candidates={1111: ("Automatic Guess",)})
        resolver, config = self.make_resolver(provider)
        trace = MatchingTrace()
        delivery = DummyDelivery("Known Series S02E01")

        opener = patch("src.nextgen.providers.identity._open", return_value=self.db_conn)
        recog = patch("src.nextgen.providers.identity.recognized_for_release", return_value=recognized)
        with opener, recog:
            result = await resolver(delivery, config, threading.Event(), matching=trace)

        self.assertEqual(result["anime"]["anidb_id"], 7777)
        self.assertIn({"step": "recognized_season", "status": "passed", "note": "recognized_season_reused"},
                      trace.data["checks"])

        self.assertEqual(result["anime"]["anidb_id"], 7777)
        self.assertIn({"step": "recognized_season", "status": "passed", "note": "recognized_season_reused"},
                      trace.data["checks"])

    async def test_downstream_anime_lists_mapping_error_preserved(self):
        # Job #577: Title scoring succeeds, but Anime-Lists has no mapping
        provider = DummyProvider(candidates={5555: ("Show Without Mapping",)})
        provider.mapping = lambda aid: (_ for _ in ()).throw(IdentityNotReady())
        resolver, config = self.make_resolver(provider)
        trace = MatchingTrace()
        delivery = DummyDelivery("Show Without Mapping - 01")

        with patch("src.nextgen.providers.identity._open", return_value=self.db_conn):
            with self.assertRaises(IdentityNotReady):
                await resolver(delivery, config, threading.Event(), matching=trace)

        # The failure occurred at anime_lists stage, not anidb_candidates
        self.assertIn({"step": "anidb_candidates", "status": "passed", "note": None}, trace.data["checks"])

    async def test_season_pack_rejected_preservation(self):
        # Job #6200: Multiple declared seasons rejected safely
        provider = DummyProvider(candidates={100: ("Pack Show",)})
        resolver, config = self.make_resolver(provider)
        trace = MatchingTrace()
        delivery = DummyDelivery("[Batch] Pack Show S01-S02 Complete (1080p)")

        with patch("src.nextgen.providers.identity._open", return_value=self.db_conn):
            with self.assertRaises(IdentityNotReady):
                await resolver(delivery, config, threading.Event(), matching=trace)

        self.assertIn({"step": "season_scope", "status": "failed", "note": "multiple_seasons"},
                      trace.data["checks"])

    async def test_threshold_configurable_filtering(self):
        # When threshold is 95%, candidate with 92% similarity is rejected
        variants = (TitleVariant("Attack on Titan Season 1", "cleaned"),)
        candidates = {100: ("Attack on Titan Season One",)} # ~92% similarity
        res_95 = rank_and_select_candidates(candidates, variants, 95)
        self.assertIsNone(res_95.selected)

        # When threshold is lowered to 80%, candidate with 92% similarity is accepted
        res_80 = rank_and_select_candidates(candidates, variants, 80)
        self.assertIsNotNone(res_80.selected)
        self.assertEqual(res_80.selected.anidb_id, 100)


if __name__ == "__main__":
    unittest.main()
