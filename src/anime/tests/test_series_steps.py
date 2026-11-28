"""The series steps with the real step code and a fake qBittorrent of each version.

qBittorrent 4 (and 5.0-5.1) adds the magnet stopped and resumes it only until
the file list arrives; 5.2+ fetches the metadata without adding. The version
adapter decides (owner, 2026-11-26: support every version).
"""
import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path

from src.anime.acquisition.metadata import BatchInventory, TorrentFile
from src.anime.acquisition.submission import ClientUnavailable, Observation
from src.anime.batch.provider_worker import BatchVerificationCompletion
from src.anime.batch.verification import VerifiedBatch, VerifiedBatchFile
from src.anime.release_contracts import VerifiedRelease
from src.anime.series_job.context import SeriesContext
from src.anime.series_job.steps.check_release import check_release
from src.anime.series_job.steps.download import download
from src.anime.series_job.steps.drop import drop_torrent
from src.anime.series_job.steps.metadata import get_metadata
from src.anime.series_job.store import SeriesRow
from src.anime.series_job.torrents import read_metainfo, save_metainfo
from src.anime.tests.test_acquisition_metadata import metadata
from src.anime.torrent_client.contract import ClientFile

RAW, HASH = metadata(name=b'Show', files=[{b'length': 10, b'path': [b'info.nfo']},
                                          {b'length': 100, b'path': [b'Show - 05.mkv']}])


class FakeQb:
    def __init__(self, strategy='guarded_export', raw=RAW):
        self.metadata_strategy, self.raw = strategy, raw
        self.torrent, self.calls, self.free, self.fetches = None, [], 10**12, 0
        self.down = False

    def _check(self):
        if self.down:
            raise ClientUnavailable()

    def fetch_metadata(self, source, digest, *, batch):
        self._check()
        self.fetches += 1
        return None if self.fetches == 1 else (None, self.raw)

    def add_series_torrent(self, digest, marker, *, magnet=None, raw=None):
        self._check()
        self.calls.append(('add', 'magnet' if magnet else 'file'))
        files = (TorrentFile(0, 'Show/info.nfo', 10), TorrentFile(1, 'Show/Show - 05.mkv', 100)) if raw else ()
        self.torrent = {'state': 'pausedDL', 'files': files, 'priorities': {0: 1, 1: 1}, 'progress': {0: 0, 1: 0}}

    def series_torrent(self, digest, marker):
        self._check()
        t = self.torrent
        return None if t is None else Observation(HASH, t['state'], '/d/ad-' + marker, 'akon',
                                                  ('ad-' + marker,), False, t['files'])

    def start_series_torrent(self, digest, marker):
        self.calls.append(('start',))
        if not self.torrent['files']:        # qBittorrent finds the file list once running.
            self.torrent['files'] = (TorrentFile(0, 'Show/info.nfo', 10), TorrentFile(1, 'Show/Show - 05.mkv', 100))
        self.torrent['state'] = 'downloading'

    def stop_series_torrent(self, digest, marker):
        self.calls.append(('stop',))
        self.torrent['state'] = 'pausedDL'

    def export_metainfo(self, digest, *, batch):
        return None, self.raw

    def select_series_files(self, digest, marker, wanted):
        self.calls.append(('select', tuple(sorted(wanted))))
        self.torrent['priorities'] = {i: int(i in wanted) for i in self.torrent['priorities']}

    def files(self, digest):
        t = self.torrent
        return tuple(ClientFile(f.index, '/d/' + f.path, f.size, t['progress'][f.index], t['priorities'][f.index] > 0)
                     for f in t['files'])

    def free_space(self):
        return self.free

    def remove_series_torrent(self, digest, marker):
        self.calls.append(('remove',))
        self.torrent = None
        return 'deleted'


class Configuration:
    def __init__(self, database):
        self.values = {'storage.database_path': database, 'user_evaluation.backfill_count': 6,
                       'user_evaluation.following_count': 1,
                       # Unset unless a test sets them; the storage mount guard skips unset folders.
                       'storage.download_root': None, 'storage.user_evaluation_root': None,
                       'storage.keep_root': None, 'anime.title_language': 'english'}

    def value(self, key):
        return self.values[key]


class Steps(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.configuration = Configuration(str(Path(temp.name) / 'akon.sqlite'))
        self.qb = FakeQb()
        self.saved = []
        qb = self.qb
        import sqlite3
        from src.anime.storage.sqlite import initialize
        db = sqlite3.connect(self.configuration.value('storage.database_path'))
        initialize(db)
        self.addCleanup(db.close)
        self.context = SeriesContext(verify=None, torrents=type('T', (), {'client': lambda s, c, m: qb})(), db=db,
                                     cancelled=threading.Event(), configuration=self.configuration, now=1000,
                                     checkpoint=lambda: self.saved.append(json.dumps(self.torrent)))
        self.series = SeriesRow(18162, 'Dungeon People', 'WORKING', None, 1)
        self.torrent = {'infohash': HASH, 'release_name': '[G] Dungeon People - 05', 'step': 'get_metadata',
                        'state': 'working', 'episodes': [], 'files': [],
                        'raw_body': json.dumps({'release_name': '[G] Dungeon People - 05', 'torrent_infohash': HASH}),
                        'identity': {'anime': {'anidb_id': 18162, 'tvdb_series_id': 438420}}}
        self.series.torrents.append(self.torrent)

    def run_step(self, step):
        self.context.now += 5000
        return asyncio.run(step(self.series, self.torrent, self.context))

    def test_qbittorrent_4_adds_the_magnet_once_stopped_and_stops_it_once_the_file_list_is_in(self):
        results = [self.run_step(get_metadata) for _ in range(4)]
        self.assertEqual([r.action for r in results], ['wait', 'wait', 'wait', 'done'])
        self.assertEqual(self.qb.calls, [('add', 'magnet'), ('start',), ('stop',)])
        self.assertIn('"added": "sent"', self.saved[0])          # Recorded before the add was sent.
        self.assertEqual(read_metainfo(self.configuration, HASH), RAW)

    def test_qbittorrent_5_2_fetches_the_metadata_without_adding(self):
        self.qb.metadata_strategy = 'fetch'
        self.assertEqual(self.run_step(get_metadata).action, 'wait')
        self.assertEqual(self.run_step(get_metadata).action, 'done')
        self.assertEqual(self.qb.calls, [])
        self.assertEqual(read_metainfo(self.configuration, HASH), RAW)

    def test_qbittorrent_down_is_a_wait_and_the_torrent_keeps_its_place(self):
        self.qb.down = True
        result = self.run_step(get_metadata)
        self.assertEqual((result.action, result.why), ('wait', 'SERVICE_UNAVAILABLE'))

    def test_a_single_episode_picks_its_one_video(self):
        save_metainfo(self.configuration, HASH, RAW)
        async def verify(source, configuration, cancelled):
            self.assertEqual(source.tvdb_series_id, 438420)          # From the identity process.
            return VerifiedRelease(source, 5, 281728, 1, 5, 10348661)
        self.context.verify = verify
        self.assertEqual(self.run_step(check_release).next_step, 'reserve_episodes')
        self.assertEqual(self.torrent['episodes'], ['S01E05'])
        self.assertEqual((self.torrent['files'][0]['index'], self.torrent['files'][0]['anidb_episode_id']), (1, 281728))

    def test_a_pack_that_starts_at_e13_takes_its_trial_from_e13(self):
        save_metainfo(self.configuration, HASH, RAW)
        files = tuple(TorrentFile(i, f'Show/Show - {13 + i:02d}.mkv', 100) for i in range(12))
        facts = tuple(VerifiedBatchFile(i, f.path, 13 + i, 13 + i, 5000 + i, 1, 13 + i, 9000 + i)
                      for i, f in enumerate(files))
        async def verify(source, configuration, cancelled):
            return BatchVerificationCompletion(source, BatchInventory(HASH, 'b' * 64, files),
                                               VerifiedBatch(HASH, 'b' * 64, 18162, 438420, 1, facts))
        self.context.verify = verify
        self.run_step(check_release)
        self.assertEqual(self.torrent['episodes'], [f'S01E{n:02d}' for n in range(13, 19)])

    def test_qbittorrent_5_2_download_adds_from_the_saved_bytes_once_then_selects_starts_and_finishes(self):
        save_metainfo(self.configuration, HASH, RAW)
        self.torrent.update(step='download', episodes=['S01E05'], qb={'marker': 'a' * 32},
                            files=[{'index': 1, 'path': 'Show/Show - 05.mkv', 'size': 100, 'episode': 'S01E05'}])
        self.series.episodes['S01E05'] = {'state': 'reserved', 'torrent': HASH, 'file': None}
        actions = [self.run_step(download).action for _ in range(3)]
        self.assertEqual(actions, ['wait'] * 3)
        self.assertEqual(self.qb.calls, [('add', 'file'), ('select', (1,)), ('start',)])
        self.assertEqual(self.series.episodes['S01E05']['state'], 'downloading')
        self.qb.torrent['progress'][1] = 0.55
        self.assertEqual(self.run_step(download).action, 'wait')
        self.assertTrue(any('downloading, 55%' in line for line in self.series.recent))
        self.qb.torrent['progress'][1] = 1
        self.assertEqual(self.run_step(download).next_step, 'check_media')
        self.assertEqual(self.torrent['files'][0]['local_path'], '/d/Show/Show - 05.mkv')

    def test_staging_full_holds_a_new_download_until_there_is_room(self):
        """Owner, 2026-11-27: nothing is downloaded that staging has no room for; it waits instead."""
        from unittest import mock
        from src.anime.series_job.steps import download as step
        from src.anime.series_job.store import get_series, update_series, wake
        staging = Path(self.configuration.value('storage.database_path')).parent / 'staging'
        staging.mkdir()
        self.configuration.values['storage.user_evaluation_root'] = str(staging)
        save_metainfo(self.configuration, HASH, RAW)
        gib = 2**30
        self.torrent.update(step='download', episodes=['S01E05'], qb={'marker': 'a' * 32},
                            files=[{'index': 1, 'path': 'Show/Show - 05.mkv', 'size': 2 * gib, 'episode': 'S01E05'}])
        self.series.episodes['S01E05'] = {'state': 'reserved', 'torrent': HASH, 'file': None}
        # Another series' torrent, started, not staged yet: 3 GB already on its way to staging.
        wake(self.context.db, 1, 'Other', now=1)
        other = get_series(self.context.db, 1)
        other.torrents.append({'infohash': 'c' * 40, 'step': 'check_media', 'state': 'working',
                               'qb': {'started': True}, 'episodes': ['S01E01'],
                               'files': [{'size': 3 * gib, 'episode': 'S01E01'}]})
        update_series(self.context.db, other, now=1)
        with mock.patch.object(step, 'staging_free', return_value=5 * gib):     # 5 - 3 - 1 margin < 2.
            result = self.run_step(download)
        self.assertEqual((result.action, result.why), ('wait', 'STAGING_FULL'))
        self.assertEqual(self.qb.calls, [])                                     # Nothing added or started.
        self.assertTrue(any('staging is full' in line for line in self.series.recent))
        with mock.patch.object(step, 'staging_free', return_value=7 * gib):     # Room now.
            self.assertEqual(self.run_step(download).action, 'wait')
        self.assertEqual(self.qb.calls, [('add', 'file')])
        self.assertIsNone(self.torrent.get('why'))

    def test_a_full_disk_waits_then_restarts_the_torrent(self):
        self.torrent.update(step='download', episodes=['S01E05'],
                            qb={'marker': 'a' * 32, 'added': 'sent', 'selected': True, 'started': True},
                            files=[{'index': 1, 'path': 'Show/Show - 05.mkv', 'size': 100, 'episode': 'S01E05'}])
        self.qb.add_series_torrent(HASH, 'a' * 32, raw=RAW)
        self.qb.select_series_files(HASH, 'a' * 32, {1})
        self.qb.torrent['state'], self.qb.free = 'error', 0
        self.assertEqual(self.run_step(download).why, 'TORRENT_DISK_FULL')
        self.qb.free = 10**12
        self.assertEqual(self.run_step(download).why, 'TORRENT_DOWNLOAD_IN_PROGRESS')
        self.assertEqual(self.qb.calls[-1], ('start',))

    def test_drop_removes_the_owned_torrent_and_frees_its_episodes(self):
        save_metainfo(self.configuration, HASH, RAW)
        self.qb.add_series_torrent(HASH, 'a' * 32, raw=RAW)
        self.torrent.update(qb={'marker': 'a' * 32, 'added': 'sent'}, episodes=['S01E05'])
        self.series.episodes['S01E05'] = {'state': 'downloading', 'torrent': HASH, 'file': None}
        asyncio.run(drop_torrent(self.series, self.torrent, 'MEDIA_AUDIO_REQUIRED', self.context))
        self.assertEqual((self.qb.calls[-1], self.series.episodes, self.torrent['state']),
                         (('remove',), {}, 'dropped'))
        self.assertIsNone(read_metainfo(self.configuration, HASH))


    def test_media_check_reads_each_file_and_drops_wrong_audio(self):
        from functools import partial
        from unittest import mock
        from src.anime.acquisition import media
        from src.anime.series_job.steps import check_media as step
        from src.anime.series_job.torrents import options_for
        from src.anime.tests.test_video_verification_hash_redesign import probe_bytes
        root = Path(self.configuration.value('storage.database_path')).parent / 'downloads'
        self.configuration.values.update({'qbittorrent.base_url': 'http://qb.test', 'storage.download_root': str(root),
                                          'qbittorrent.category': 'akon', 'qbittorrent.timeout_seconds': 5,
                                          'acquisition.audio_check': 'japanese_english'})
        folder = Path(options_for(self.configuration, 'a' * 32).download_root) / 'Show'
        folder.mkdir(parents=True)
        (folder / 'Show - 05.mkv').write_bytes(b'v' * 100)
        self.torrent.update(step='check_media', episodes=['S01E05'], qb={'marker': 'a' * 32},
                            files=[{'index': 1, 'path': 'Show/Show - 05.mkv', 'size': 100, 'episode': 'S01E05'}])
        self.series.episodes['S01E05'] = {'state': 'downloading', 'torrent': HASH, 'file': None}
        for languages, expected in ((('jpn',), 'drop'), (('jpn', 'eng'), 'done')):
            self.torrent['files'][0].pop('media', None)
            fake = partial(media.verify_path, read_probe=lambda fd, flag: probe_bytes(languages=languages))
            with mock.patch.object(step, 'verify_path', fake):
                result = self.run_step(step.check_media)
            self.assertEqual(result.action, expected, languages)
        self.assertEqual(result.why, None)
        self.assertEqual(self.torrent['files'][0]['media']['languages'], ['eng', 'jpn'])
        self.assertEqual(self.series.episodes['S01E05']['state'], 'verified')


    def staged(self, *, transcode=False, transcoder=None, stop_after_transcode=False):
        import sqlite3
        from src.anime.series_job import store
        from src.anime.series_job.steps.stage import stage
        from src.anime.series_job.torrents import options_for
        from src.anime.storage.sqlite import initialize
        base = Path(self.configuration.value('storage.database_path')).parent
        db = sqlite3.connect(self.configuration.value('storage.database_path'))
        initialize(db)
        store.wake(db, 18162, 'Dungeon People', now=1)
        db.commit()
        claim, row = store.claim(db, now=2)
        db.commit()
        row.torrents = [self.torrent]
        self.series = row
        self.configuration.values.update({
            'qbittorrent.base_url': 'http://qb.test', 'qbittorrent.category': 'akon', 'qbittorrent.timeout_seconds': 5,
            'storage.download_root': str(base / 'downloads'), 'storage.user_evaluation_root': str(base / 'staging'),
            'storage.keep_root': str(base / 'keep')})
        self.configuration.desired_revision = 3
        (base / 'staging').mkdir()
        source = Path(options_for(self.configuration, 'a' * 32).download_root) / 'Show'
        source.mkdir(parents=True)
        (source / 'Show - 05.mkv').write_bytes(b'v' * 100)
        from src.anime.acquisition.media import verify_path
        from src.anime.tests.test_video_verification_hash_redesign import probe_bytes
        checked = verify_path(str(source.parent), 'Show/Show - 05.mkv', 100, threading.Event(),
                              read_probe=lambda fd, flag: probe_bytes())
        self.torrent.update(step='stage', episodes=['S01E05'], qb={'marker': 'a' * 32},
                            files=[{'index': 1, 'path': 'Show/Show - 05.mkv', 'size': 100, 'episode': 'S01E05',
                                    'anidb_episode_id': 281728, 'tvdb_episode_id': 10348661, 'media': checked}])
        row.episodes['S01E05'] = {'state': 'verified', 'torrent': HASH, 'file': None}
        self.context.claim = claim
        self.context.checkpoint = lambda: store.save(db, claim, row, now=3)
        self.configuration.values['transcode.mode'] = 'remote' if transcode else 'off'
        def fake_transcoder(source, output, configuration, secrets, flag, *, started, attempts):
            started()
            Path(output).write_bytes(b't' * 40)
            return True
        self.context.transcoder = transcoder or fake_transcoder
        from src.anime.series_job.steps.transcode import transcode as transcode_step
        if transcoder is not None:
            result = self.run_step(transcode_step)
            entry = json.loads(db.execute('SELECT torrents_json FROM series').fetchone()[0])[0]['files'][0]
            db.close()
            return result, entry
        self.assertEqual(self.run_step(transcode_step).next_step, 'stage')
        if stop_after_transcode:
            result = self.run_step(stage)
            db.close()
            return result
        self.assertEqual(self.run_step(stage).next_step, 'done')
        staged = row.episodes['S01E05']['file']
        self.assertEqual(staged, 'Dungeon People [tvdbid-438420]/Season 01/Dungeon People - S01E05.mkv')
        self.assertEqual((base / 'staging' / staged).read_bytes(), b't' * 40 if transcode else b'v' * 100)
        self.assertTrue((base / 'staging' / 'Dungeon People [tvdbid-438420]/tvshow.nfo').exists())
        self.assertTrue((source / 'Show - 05.mkv').exists())                  # COPY: qBittorrent keeps seeding.
        saved = json.loads(db.execute('SELECT torrents_json FROM series').fetchone()[0])[0]['staging']['artifacts']
        self.assertEqual({a['phase'] for a in saved}, {'PUBLISHED'})          # Its records are in the row.
        self.assertEqual(json.loads(db.execute('SELECT torrents_json FROM series').fetchone()[0])[0]['files'][0]
                         .get('transcode_attempts'), 1 if transcode else None)   # Saved before the try.
        self.assertFalse(list((base / 'staging' / '.transcode').glob('*')) if transcode else [])
        # Every staged video has a content id, with transcode on or off (owner, 2026-11-27).
        from src.anime.acquisition.media import path_content_id
        files = json.loads(db.execute('SELECT torrents_json FROM series').fetchone()[0])[0]['files'][0]
        self.assertIsNotNone(self.torrent['files'][0]['media'].get('content'))      # From the media check.
        self.assertEqual(self.torrent['files'][0]['staged_content'],
                         path_content_id(str(base / 'staging'), staged, 40 if transcode else 100))
        db.close()


    def test_staging_copies_the_checked_file_with_its_nfos_and_keeps_the_torrents_copy(self):
        self.staged()

    def test_a_transcoded_file_is_moved_into_staging_and_the_torrents_copy_stays(self):
        self.staged(transcode=True)

    def test_a_transcode_that_fails_on_every_agent_stops_the_torrent_and_keeps_it(self):
        from src.anime.services.transcode import TranscodeFailed
        from src.anime.series_job.steps.transcode import transcode
        self.configuration.values.update({'transcode.mode': 'remote', 'storage.user_evaluation_root': '/unused'})
        self.torrent.update(files=[{'index': 1, 'path': 'Show/Show - 05.mkv', 'size': 100, 'episode': 'S01E05'}],
                            episodes=['S01E05'])
        from unittest import mock
        from src.anime.series_job.steps import transcode as step
        with mock.patch.object(step, 'transcode_file', side_effect=TranscodeFailed('all agents failed')):
            result = self.run_step(transcode)
        # Owner, 2026-11-27: every transcoder failing may be the transcoders; the release is kept.
        self.assertEqual(result.action, 'stop')
        self.assertTrue(result.why.startswith('TRANSCODE_FAILED'))

    def test_our_share_failing_during_a_transcode_waits_and_gives_the_attempt_back(self):
        from src.anime.services.transcode import TranscodeStorageUnavailable
        def failing(source, output, configuration, secrets, flag, *, started, attempts):
            started()                                     # The attempt was spent before the work...
            raise TranscodeStorageUnavailable('ESTALE on the staging share')   # ...then our share failed.
        result, entry = self.staged(transcode=True, transcoder=failing)
        self.assertEqual(result.action, 'wait')
        self.assertTrue(result.why.startswith('STORAGE_UNAVAILABLE'))
        self.assertEqual(entry.get('transcode_attempts'), 0)                  # Given back, and saved.

    def test_a_staging_file_that_does_not_match_stops_the_torrent_and_keeps_it(self):
        from unittest import mock
        from src.anime.staging import Publisher, StagingReview
        with mock.patch.object(Publisher, 'publish', side_effect=StagingReview('a file staging did not write')):
            result = self.staged(stop_after_transcode=True)
        self.assertEqual(result.action, 'stop')
        self.assertIn('STAGING_REVIEW', result.why)

    def test_storage_trouble_is_told_apart_from_the_transcoders(self):
        import errno, socket
        from src.anime.services.transcode import storage_trouble
        agent = object()
        self.assertTrue(storage_trouble(OSError(errno.ESTALE, 'stale'), agent))          # Our share.
        self.assertTrue(storage_trouble(FileNotFoundError(2, 'gone'), agent))
        self.assertTrue(storage_trouble(OSError(errno.EIO, 'io'), None))                 # Local: always ours.
        self.assertFalse(storage_trouble(ConnectionRefusedError(), agent))               # The agent's.
        self.assertFalse(storage_trouble(TimeoutError(), agent))
        self.assertFalse(storage_trouble(socket.gaierror(), agent))
        self.assertFalse(storage_trouble(OSError(errno.EHOSTUNREACH, 'no route'), agent))
        self.assertFalse(storage_trouble(ValueError('bad reply'), agent))


    def test_a_single_episode_wants_its_trial_window_and_the_search_adds_the_rest(self):
        from types import SimpleNamespace
        from src.anime.acquisition.prowlarr_search import DiscoveryPolicy, Release
        from src.anime.series_job import search
        save_metainfo(self.configuration, HASH, RAW)
        async def verify(source, configuration, cancelled):
            return VerifiedRelease(source, 5, 281728, 1, 5, 10348661)
        self.context.verify = verify
        self.run_step(check_release)
        self.assertEqual(self.series.work['search']['wanted'], [1, 2, 3, 4, 5, 6])   # Backfill 6, following 1.

        class FakeProwlarr:
            policy = DiscoveryPolicy(min_seeders=1)
            asked = []
            def backfill(self, aid, tid, numbers, *, select_candidates, tried_by_episode):
                self.asked.append(numbers)
                def found(n):
                    if n == 6:
                        return SimpleNamespace(number=n, selection=None)
                    digest = f'{n:040x}'
                    return SimpleNamespace(number=n, selection=SimpleNamespace(release=Release(
                        f'[G] Dungeon People - {n:02d}', 'Nyaa', 1080, 9, 300_000_000, None, digest,
                        f'magnet:?xt=urn:btih:{digest}', eligible_for_handoff=True)))
                return tuple(found(n) for n in numbers)
        prowlarr = FakeProwlarr()
        self.assertEqual(search.run(self.series, self.context, prowlarr), 4)
        self.assertEqual(prowlarr.asked, [(1, 2, 3, 4), (6,)])                     # E05 is covered.
        added = [t for t in self.series.torrents if t.get('found_for')]
        self.assertEqual([t['step'] for t in added], ['get_metadata'] * 4)
        self.assertEqual(added[0]['identity']['anime']['tvdb_series_id'], 438420)
        self.assertEqual(self.series.work['search']['due_ms'], self.context.now + search.RETRY_MS)  # E06 later.
        self.assertEqual(search.run(self.series, self.context, prowlarr), 0)    # Already tried: nothing new.


if __name__ == '__main__':
    unittest.main()
