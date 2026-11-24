"""After a Keep, AKON finds dubs for the episodes Sonarr is missing and pushes them to Sonarr."""
import unittest
from types import SimpleNamespace
from unittest import mock

from src.anime.acquisition.prowlarr_search import DiscoveryPolicy, EpisodeTarget, Release
from src.anime.jobs import find_dubs_job
from src.anime.sonarr import Client, Options


def release(title, digest, seeders=5):
    return Release(title, 'Nyaa', 1080, seeders, 300_000_000, None, digest, f'magnet:?xt=urn:btih:{digest}',
                   eligible_for_handoff=True)


class FakeSonarr:
    def __init__(self, missing, rejects=()):
        self.missing, self.rejects, self.pushed = missing, set(rejects), []

    def series(self, tvdb): return {'id': 132, 'title': 'Dungeon People'}
    def missing_episodes(self, series_id): return self.missing

    def push_release(self, title, magnet, digest, indexer, size, seeders):
        self.pushed.append(title)
        return ['Custom Formats have score 0'] if title in self.rejects else []


class FakeProwlarr:
    policy = DiscoveryPolicy(min_seeders=1)

    def __init__(self, found):
        self.found, self.asked = found, []

    def backfill(self, aid, tid, numbers, *, select_candidates):
        self.asked.append(numbers)
        return tuple(SimpleNamespace(number=n, status='CANDIDATES' if self.found.get(n) else 'WAITING_FOR_DUB',
                                     selection=SimpleNamespace(result=SimpleNamespace(candidates=self.found[n]))
                                     if self.found.get(n) else None) for n in numbers)


def catalog(aid, tid):
    return SimpleNamespace(episodes={n: EpisodeTarget(n, 'SEARCHABLE', n, 900 + n, n, 1) for n in range(1, 13)})


class FindDubsTests(unittest.TestCase):
    def test_pushes_a_dub_for_each_missing_episode_and_tries_the_next_when_sonarr_rejects(self):
        sonarr = FakeSonarr({(1, 7), (1, 8)}, rejects={'Show - 07 [DUAL-VARYG]'})
        prowlarr = FakeProwlarr({7: (release('Show - 07 [DUAL-VARYG]', 'a' * 40),
                                     release('Show - 07 [Dual Audio English]', 'b' * 40)),
                                 8: ()})
        grabbed, not_found = find_dubs_job.find_and_push(sonarr, prowlarr, catalog, 18162, 438420)
        self.assertEqual(prowlarr.asked, [(7, 8)])          # Only what Sonarr is missing is searched.
        self.assertEqual(sonarr.pushed, ['Show - 07 [DUAL-VARYG]', 'Show - 07 [Dual Audio English]'])
        self.assertEqual((grabbed, not_found), (['Dungeon People S01E07'], ['S01E08']))

    def test_a_season_pack_is_pushed_once_for_all_its_episodes(self):
        pack = release('Show S01 [Dual Audio English]', 'c' * 40)
        sonarr = FakeSonarr({(1, 7), (1, 8)})
        grabbed, _ = find_dubs_job.find_and_push(sonarr, FakeProwlarr({7: (pack,), 8: (pack,)}), catalog, 1, 2)
        self.assertEqual((sonarr.pushed, len(grabbed)), ([pack.title], 2))

    def test_nothing_missing_searches_nothing(self):
        prowlarr = FakeProwlarr({})
        self.assertEqual(find_dubs_job.find_and_push(FakeSonarr(set()), prowlarr, catalog, 1, 2), ([], []))
        self.assertEqual(prowlarr.asked, [])


class AudioCheckTests(unittest.IsolatedAsyncioTestCase):
    async def run_with(self, audio):
        asked = []
        configuration = mock.Mock(value=lambda key: {'acquisition.audio_check': audio}[key])
        context = mock.Mock(configuration=configuration, data=lambda: {'anidb_id': 1, 'tvdb_series_id': 2})
        job = find_dubs_job.registration(mock.Mock(), catalog_factory=lambda c: asked.append('catalog'),
                                         sonarr_factory=lambda *a: asked.append('sonarr'),
                                         prowlarr_factory=lambda *a, **k: mock.Mock())
        with mock.patch.object(find_dubs_job, 'find_and_push', return_value=([], [])):
            outcome = await job.execute(context)
        return outcome.kind, asked

    async def test_nothing_is_done_unless_the_audio_check_wants_english(self):
        for audio in ('off', 'japanese'):
            self.assertEqual(await self.run_with(audio), ('complete', []))
        self.assertEqual((await self.run_with('japanese_english'))[1], ['catalog', 'sonarr'])


class SonarrCallsTests(unittest.TestCase):
    def client(self, answers):
        client = Client(Options('http://sonarr.test', '/a', '/a', '/a', 0, 5), 'k' * 32)
        client.request = mock.Mock(side_effect=lambda path, **kwargs: answers[path])
        return client

    def test_missing_means_monitored_aired_without_a_file_and_not_downloading(self):
        client = self.client({'queue': {'records': [{'episodeId': 3}]}, 'episode': [
            {'id': 1, 'seasonNumber': 1, 'episodeNumber': 7, 'monitored': True, 'hasFile': False, 'airDateUtc': '2024-08-23T00:00:00Z'},
            {'id': 2, 'seasonNumber': 1, 'episodeNumber': 6, 'monitored': True, 'hasFile': True, 'airDateUtc': '2024-08-16T00:00:00Z'},
            {'id': 3, 'seasonNumber': 1, 'episodeNumber': 8, 'monitored': True, 'hasFile': False, 'airDateUtc': '2024-08-30T00:00:00Z'},
            {'id': 4, 'seasonNumber': 1, 'episodeNumber': 9, 'monitored': False, 'hasFile': False, 'airDateUtc': '2024-09-06T00:00:00Z'},
            {'id': 5, 'seasonNumber': 1, 'episodeNumber': 13, 'monitored': True, 'hasFile': False, 'airDateUtc': '2099-01-01T00:00:00Z'},
            {'id': 6, 'seasonNumber': 0, 'episodeNumber': 1, 'monitored': True, 'hasFile': False, 'airDateUtc': '2024-01-01T00:00:00Z'}]})
        self.assertEqual(client.missing_episodes(132), {(1, 7)})

    def test_push_returns_sonarrs_rejections(self):
        self.assertEqual(self.client({'release/push': [{'approved': True}]}).push_release('t', 'm', 'h', 'i', 1, 1), [])
        self.assertEqual(self.client({'release/push': [{'approved': False, 'rejections': ['score 0']}]})
                         .push_release('t', 'm', 'h', 'i', 1, 1), ['score 0'])


if __name__ == '__main__':
    unittest.main()
