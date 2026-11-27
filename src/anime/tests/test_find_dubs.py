"""After a Keep, AKON finds dubs for the episodes Sonarr is missing and pushes them to Sonarr."""
import unittest
from types import SimpleNamespace
from unittest import mock

from src.anime.acquisition.prowlarr_search import DiscoveryPolicy, EpisodeTarget, Release
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
