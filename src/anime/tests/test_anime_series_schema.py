"""AnimeSeries v2 schema / identity-completion gate tests; offline-only."""
import copy
import json
import unittest
from dataclasses import fields
from pathlib import Path

from src.anime.schema.anime_series import (
    AnimeSeries, Anime, Episode, TVDBPlacement, FIELD_DOC_KEYS
)

FIXTURES = Path(__file__).parent / "fixtures"


class AnimeSeriesSchemaTests(unittest.TestCase):
    def fixture(self, name="anime_series_v2_verified.json"):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def test_complete_identity_roundtrip_is_stable(self):
        for name in ("anime_series_v2_verified.json",
                     "anime_series_v2_continuous.json"):
            with self.subTest(name=name):
                original = self.fixture(name)
                series = AnimeSeries.from_dict(original)
                self.assertEqual(series.to_dict(), original)
                self.assertEqual(AnimeSeries.from_json(series.to_json()), series)

    def test_every_field_has_documentation(self):
        for cls in (AnimeSeries, Anime, Episode, TVDBPlacement):
            for f in fields(cls):
                with self.subTest(cls=cls.__name__, field=f.name):
                    self.assertEqual(set(f.metadata), FIELD_DOC_KEYS)
                    self.assertTrue(all(isinstance(f.metadata[k], str) and
                                        f.metadata[k] for k in FIELD_DOC_KEYS))

    def test_completion_gate_must_have_all_required_series_identity(self):
        data = self.fixture()
        invalids = [
            lambda x: x["anime"].update(anidb_id=None),
            lambda x: x["anime"].update(tvdb_series_id=None),
            lambda x: x["anime"].update(tvdb_series_id=0),
            lambda x: x["anime"].pop("tvdb_series_id"),
            lambda x: x["anime"].update(title="  "),
            lambda x: x["anime"].pop("title"),
            lambda x: x["episodes"][0].update(anidb_episode_id=None),
            lambda x: x["episodes"][0].pop("anidb_episode_id"),
        ]
        for i, mutate in enumerate(invalids):
            with self.subTest(case=i), self.assertRaises(ValueError):
                candidate = copy.deepcopy(data)
                mutate(candidate)
                AnimeSeries.from_dict(candidate)

    def test_optional_data_missing_does_not_break_completed_identity(self):
        data = self.fixture("anime_series_v2_continuous.json")
        for field in ("english_title", "original_title", "type", "year"):
            del data["anime"][field]
        for episode in data["episodes"]:
            episode.pop("tvdb_placement")
            episode.pop("anidb_episode_number")
            episode.pop("title")
            episode.pop("type")
        data.pop("attributes")
        series = AnimeSeries.from_dict(data)
        self.assertEqual(series.episodes[0].tvdb_placement, None)
        self.assertEqual(series.to_dict()["episodes"][0]["tvdb_placement"], None)
        self.assertEqual(series.to_dict()["attributes"], {})
        self.assertEqual(series.episodes[1].anidb_episode_id, 90002)

    def test_series_only_pack_is_valid_without_episode_claims(self):
        data = self.fixture("anime_series_v2_series_only.json")
        identified = AnimeSeries.from_dict(data)
        self.assertEqual(identified.episodes, ())
        self.assertEqual(identified.anime.tvdb_series_id, 305089)
        self.assertEqual(identified.to_dict(), data)
        self.assertEqual(AnimeSeries.from_json(identified.to_json()), identified)

    def test_omitted_episodes_normalize_without_loosening_series_identity(self):
        data = self.fixture("anime_series_v2_series_only.json")
        data.pop("episodes")
        identified = AnimeSeries.from_dict(data)
        self.assertEqual(identified.to_dict()["episodes"], [])
        self.assertEqual(identified.anime.tvdb_series_id, 305089)
        self.assertEqual(identified.anime.anidb_id, 19242)

    def test_present_episodes_still_require_exact_anidb_id(self):
        data = self.fixture("anime_series_v2_series_only.json")
        for episode in ({}, {"anidb_episode_id": None},
                        {"anidb_episode_id": 0},
                        {"anidb_episode_id": "90000"}):
            with self.subTest(episode=episode), self.assertRaises(ValueError):
                candidate = copy.deepcopy(data)
                candidate["episodes"] = [episode]
                AnimeSeries.from_dict(candidate)
        data["episodes"] = [
            {"anidb_episode_id": 90001},
            {"anidb_episode_id": 90002},
        ]
        self.assertEqual(len(AnimeSeries.from_dict(data).episodes), 2)

    def test_episode_field_is_optional_but_must_be_array_when_present(self):
        candidate = self.fixture("anime_series_v2_series_only.json")
        for invalid in (None, {}, "unknown", [None]):
            with self.subTest(episodes=invalid), self.assertRaises(ValueError):
                AnimeSeries.from_dict({**candidate, "episodes": invalid})

    def test_tvdb_series_id_is_required_and_must_be_valid(self):
        data = self.fixture("anime_series_v2_series_only.json")
        missing = copy.deepcopy(data)
        missing["anime"].pop("tvdb_series_id")
        with self.assertRaises(ValueError):
            AnimeSeries.from_dict(missing)
        for invalid in (None, 0, -1, "305089", True):
            with self.subTest(tvdb_id=invalid), self.assertRaises(ValueError):
                candidate = copy.deepcopy(data)
                candidate["anime"]["tvdb_series_id"] = invalid
                AnimeSeries.from_dict(candidate)
        self.assertEqual(AnimeSeries.from_dict(data).anime.tvdb_series_id, 305089)

    def test_v1_is_not_mistaken_for_v2(self):
        for name in ("anime_series_v1_verified.json",
                     "anime_series_v1_continuous.json"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                AnimeSeries.from_dict(self.fixture(name))

    def test_no_forced_tvdb_season_and_no_automatic_numbering(self):
        data = self.fixture("anime_series_v2_continuous.json")
        series = AnimeSeries.from_dict(data)
        self.assertIsNone(series.episodes[0].tvdb_placement)
        self.assertEqual(series.episodes[0].anidb_episode_number, 1000)

    def test_same_episode_number_allowed_across_distinct_anidb_anime(self):
        first = self.fixture()
        sequel = copy.deepcopy(first)
        sequel["anime"]["anidb_id"] = 67890
        sequel["episodes"][0]["anidb_episode_id"] = 99999
        sequel["episodes"][0]["anidb_episode_number"] = 1
        first["episodes"][0]["anidb_episode_number"] = 1
        self.assertEqual(AnimeSeries.from_dict(first).anime.tvdb_series_id,
                         AnimeSeries.from_dict(sequel).anime.tvdb_series_id)
        self.assertNotEqual(AnimeSeries.from_dict(first).anime.anidb_id,
                            AnimeSeries.from_dict(sequel).anime.anidb_id)

    def test_duplicate_episode_ids_within_one_series_rejected(self):
        candidate = self.fixture()
        candidate["episodes"].append(copy.deepcopy(candidate["episodes"][0]))
        with self.assertRaises(ValueError):
            AnimeSeries.from_dict(candidate)

    def test_reject_invalid_optional_mapping_but_accept_null(self):
        candidate = self.fixture()
        for placement in ({"season": 1}, {"season": None, "episode": 1},
                          {"season": -1, "episode": 1},
                          {"season": 1, "episode": 0},
                          {"season": 1, "episode": 1, "invented": 9}):
            with self.subTest(placement=placement), self.assertRaises(ValueError):
                candidate["episodes"][0]["tvdb_placement"] = placement
                AnimeSeries.from_dict(candidate)

    def test_reject_schema_drift_and_non_json_attributes(self):
        candidate = self.fixture()
        for update in ({"schema_version": 3}, {"job_id": 5},
                       {"intake": {"notification_id": 99}}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                AnimeSeries.from_dict({**candidate, **update})
        with self.assertRaises(ValueError):
            AnimeSeries.from_dict({**candidate, "attributes": {"unsafe": float("nan")}})
        with self.assertRaises(ValueError):
            AnimeSeries.from_dict({**candidate, "attributes": {"bad": object()}})

    def test_received_release_is_not_series_field(self):
        data = self.fixture()
        data["anime"]["release_name"] = "[Group] not the identified title"
        with self.assertRaises(ValueError):
            AnimeSeries.from_dict(data)


if __name__ == "__main__":
    unittest.main()
