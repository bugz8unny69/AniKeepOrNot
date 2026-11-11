"""WI-12: an existing torrent's file list supports evaluation without rediscovery or adoption."""
import asyncio
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from src.anime.acquisition.metadata import ObservedBatchInventory, TorrentFile
from src.anime.acquisition.metadata_service import METADATA_TIMEOUT_MS, shared_limiter
from src.anime.acquisition.metainfo_source import MetadataClientUnsupported, make_batch_reader
from src.anime.acquisition.submission import Observation
from src.anime.configuration import ConfigurationSnapshot
from src.anime.storage.sqlite import initialize
from src.anime.tests.test_batch_candidate_coordination import AID, TID, Fixture, digest

DIGEST = 'c' * 40
FILES = (TorrentFile(0, 'Cyberpunk Edgerunners/E01.mkv', 1_000_000),
         TorrentFile(1, 'Cyberpunk Edgerunners/E02.mkv', 1_000_000))


class FakeQb:
    """Read-only qBittorrent: any call other than login/inspect fails the test."""
    def __init__(self, strategy, rows):
        self.metadata_strategy, self.rows, self.calls, self.major = strategy, list(rows), [], 4

    def login(self):
        self.calls.append('login')

    def inspect(self, expected, *, include_files=True):
        self.calls.append('inspect')
        return self.rows.pop(0) if self.rows else None

    def __getattr__(self, name):
        raise AssertionError(f'unexpected qBittorrent call: {name}')


def existing(state='stoppedUP', files=FILES):
    # Someone else's torrent: another category, no AniKeepOrNot tag.
    return Observation(DIGEST, state, '/downloads/other', 'tv', (), False, files)


class ExistingMetadataEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'q.sqlite'
        db = sqlite3.connect(self.path)
        initialize(db)
        db.close()
        self.config = ConfigurationSnapshot(1, {'storage': {'database_path': str(self.path),
                                                            'download_root': '/srv/downloads'},
                                                'qbittorrent': {'base_url': 'http://qb:8080'}})
        self.secrets = SimpleNamespace(qbittorrent_credentials=lambda: SimpleNamespace(username='u', password='p'))

    async def read(self, client, **kwargs):
        reader = make_batch_reader(client_factory=lambda options, credentials: client, secrets=self.secrets)
        return await reader(DIGEST, self.config, threading.Event(), **kwargs)

    async def test_an_existing_external_torrent_is_evaluated_read_only(self):
        for strategy in ('legacy', 'fetch'):
            with self.subTest(strategy=strategy):
                client = FakeQb(strategy, [existing(), existing()])
                inventory = await self.read(client, evaluation=True)
                self.assertEqual(inventory, ObservedBatchInventory(DIGEST, FILES))
                self.assertEqual(client.calls, ['login', 'inspect', 'inspect'])   # No fetch, stop, tag or add.

    async def test_without_evaluation_the_normal_ownership_path_still_applies(self):
        client = FakeQb('legacy', [existing(), existing()])
        with self.assertRaises(MetadataClientUnsupported):   # No lease: never admitted, never adopted.
            await self.read(client)
        self.assertEqual(client.calls, ['login'])

    async def test_resolving_metadata_falls_back_to_the_normal_path(self):
        client = FakeQb('legacy', [existing('metaDL', ())])
        with self.assertRaises(MetadataClientUnsupported):
            await self.read(client, evaluation=True)
        self.assertEqual(client.calls, ['login', 'inspect'])

    async def test_a_journaled_hash_never_takes_the_read_only_shortcut(self):
        client = FakeQb('legacy', [existing(), existing()])
        with mock.patch('src.anime.storage.batch_metadata_admission.admitted', return_value=True), \
                self.assertRaises(MetadataClientUnsupported):
            await self.read(client, evaluation=True)
        self.assertEqual(client.calls, ['login'])

    def test_timeout_and_shared_concurrency_are_unchanged(self):
        self.assertEqual(METADATA_TIMEOUT_MS, 10 * 60 * 1000)
        self.assertEqual(shared_limiter()._limit, 2)


class VerifierEvaluationTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def test_the_release_verifier_reads_metadata_for_evaluation(self):
        import json
        from src.anime.batch.provider_worker import make_batch_verifier
        from src.anime.providers.http import Credentials, Options
        from src.anime.release_contracts import ReleaseInput
        event, job, title = self.release(1)
        seen = []

        async def reader(expected, configuration, cancelled, *, claim=None, event_id=None, evaluation=False):
            seen.append(evaluation)
            raise asyncio.CancelledError()
        verifier = make_batch_verifier(Options('registeredclient', 1), lambda: Credentials('private'), reader,
                                       client_factory=lambda *a: None)
        source = ReleaseInput(event, 1, AID, TID, json.dumps({'release_name': title,
                                                              'torrent_infohash': digest(1)}).encode())
        config = ConfigurationSnapshot(1, {'storage': {'database_path': str(self.path)}})
        with self.assertRaises(asyncio.CancelledError):
            await verifier(source, config, threading.Event(), claim=SimpleNamespace(job_id=job))
        self.assertEqual(seen, [True])


if __name__ == '__main__':
    unittest.main()
