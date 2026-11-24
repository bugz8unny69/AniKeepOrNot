"""Tests for SJ-4a: check_release step (single episode and season pack)."""
import asyncio
from dataclasses import dataclass
import unittest
from types import SimpleNamespace

from src.anime.batch.provider_worker import BatchVerificationCompletion
from src.anime.release_contracts import ReleaseInput, VerifiedRelease, ReleaseNotReady
from src.anime.acquisition.settings import SizeLimitExceeded
from src.anime.providers.errors import ProviderUnavailable
from src.anime.series_job.steps.check_release import check_release
from src.anime.series_job.store import SeriesRow


class CheckReleaseStepTests(unittest.TestCase):
    def setUp(self):
        self.series = SeriesRow(
            anidb_id=16999,
            title="Cyberpunk: Edgerunners",
            state="WORKING",
            wake_at_ms=None,
            torrents_at_once=1,
        )

    def test_single_episode_checked_successfully(self):
        raw = b'{"release_name": "[SubsPlease] Cyberpunk - 05 (1080p).mkv", "torrent_infohash": "a"*40}'
        torrent = {
            "infohash": "a" * 40,
            "release_name": "[SubsPlease] Cyberpunk - 05 (1080p).mkv",
            "raw_body": raw,
            "step": "check_release",
            "state": "waiting",
        }

        async def fake_verify(source, config, cancelled):
            return VerifiedRelease(source, anidb_episode_number=5, anidb_episode_id=50,
                                   tvdb_season=1, tvdb_episode=5, tvdb_episode_id=500)

        context = SimpleNamespace(verify=fake_verify, configuration=None, cancelled=lambda: False, now=1000)
        result = asyncio.run(check_release(self.series, torrent, context))

        self.assertEqual(result.action, "done")
        self.assertEqual(result.next_step, "reserve_episodes")
        self.assertEqual(torrent["episodes"], ["S01E05"])
        self.assertEqual(torrent["step"], "reserve_episodes")
        self.assertTrue(any("S01E05" in line for line in self.series.recent))

    def test_season_pack_checked_successfully(self):
        raw = b'{"release_name": "[SubsPlease] Cyberpunk (Batch 01-10) [1080p]", "torrent_infohash": "b"*40}'
        torrent = {
            "infohash": "b" * 40,
            "release_name": "[SubsPlease] Cyberpunk (Batch 01-10) [1080p]",
            "raw_body": raw,
            "step": "check_release",
            "state": "waiting",
        }

        @dataclass(frozen=True)
        class FakeEp:
            tvdb_season: int
            tvdb_episode: int
            anidb_episode_number: int

        @dataclass(frozen=True)
        class FakeBatch:
            episodes: list

        async def fake_verify(source, config, cancelled):
            eps = [FakeEp(1, i, i) for i in range(1, 11)]
            return SimpleNamespace(verified=FakeBatch(episodes=eps), episodes=[f"S01E{i:02d}" for i in range(1, 11)])

        context = SimpleNamespace(verify=fake_verify, configuration=None, cancelled=lambda: False, now=1000)
        result = asyncio.run(check_release(self.series, torrent, context))

        self.assertEqual(result.action, "done")
        self.assertEqual(result.next_step, "reserve_episodes")
        self.assertEqual(len(torrent["episodes"]), 10)
        self.assertEqual(torrent["episodes"][0], "S01E01")
        self.assertEqual(torrent["episodes"][-1], "S01E10")

    def test_movie_release_dropped(self):
        torrent = {
            "infohash": "c" * 40,
            "release_name": "[Group] Show The Movie (1080p)",
            "raw_body": b'{"release_name": "[Group] Show The Movie (1080p)"}',
            "step": "check_release",
            "state": "waiting",
        }
        context = SimpleNamespace(verify=None, configuration=None, cancelled=lambda: False, now=1000)
        result = asyncio.run(check_release(self.series, torrent, context))

        self.assertEqual(result.action, "drop")
        self.assertEqual(result.why, "RELEASE_MOVIE_SKIPPED")
        self.assertTrue(any("movie" in line.lower() for line in self.series.recent))

    def test_expected_errors_handled(self):
        torrent = {
            "infohash": "d" * 40,
            "release_name": "[Group] Show Special 01",
            "raw_body": b'{"release_name": "[Group] Show Special 01"}',
            "step": "check_release",
            "state": "waiting",
        }

        async def verify_unsupported(source, config, cancelled):
            raise ReleaseNotReady("RELEASE_SPECIAL_UNSUPPORTED")

        context = SimpleNamespace(verify=verify_unsupported, configuration=None, cancelled=lambda: False, now=1000)
        result = asyncio.run(check_release(self.series, torrent, context))
        self.assertEqual(result.action, "drop")
        self.assertEqual(result.why, "RELEASE_SPECIAL_UNSUPPORTED")

        # Provider temporary outage -> wait
        async def verify_outage(source, config, cancelled):
            raise ProviderUnavailable()

        context2 = SimpleNamespace(verify=verify_outage, configuration=None, cancelled=lambda: False, now=2000)
        result2 = asyncio.run(check_release(self.series, torrent, context2))
        self.assertEqual(result2.action, "wait")
        self.assertEqual(result2.wake_at_ms, 62000)


if __name__ == "__main__":
    unittest.main()
