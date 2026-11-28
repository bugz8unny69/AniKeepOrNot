"""Keep through the series worker: Sonarr owns the series, then cleanup; then KEPT (owner, 2026-11-26)."""
import asyncio
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from src.anime.acquisition.media import signature
from src.anime.series_job.actions import keep_series_sync, retry_series_sync
from src.anime.series_job.store import add_torrent, get_series, update_series, wake
from src.anime.series_job.worker import run_series_attempt
from src.anime.storage.sqlite import initialize
from src.anime.tests.test_series_steps import Configuration, FakeQb

STAGED = 'Dungeon People [tvdbid-438420] [anidbid-18162]/Season 01/Dungeon People - S01E05.mkv'


class FakeSonarr:
    def __init__(self, test):
        self.test, self.sent, self.has_series, self.command = test, [], False, 'queued'
        self.imports = True            # False: Sonarr completes the command and imports nothing.

    def __call__(self, options, key):
        self.options = options
        return self

    def request(self, path, body=None, query=None):
        if path == 'system/status':
            return {'version': '4.0.14'}
        if body is not None:
            self.sent.append((path, body.get('name') if isinstance(body, dict) else None))
            if path == 'series':
                self.has_series = True
                return {'id': 7}
            return {'id': 70 + len(self.sent)}
        if path.startswith('command/'):
            return {'status': self.command}
        if path.startswith('episodefile/'):
            return {'id': 9}
        raise AssertionError(path)

    def series(self, tvdb):
        return {'id': 7, 'path': self.options.library_root + '/Dungeon People'} if self.has_series else None

    def add_body(self, tvdb, monitor):
        return {'tvdbId': tvdb, 'monitor': monitor}

    def episode(self, selection, series):
        done = self.command == 'completed' and self.imports
        return {'id': 55, 'episodeFileId': 9 if done else 0, 'hasFile': done}

    def import_payload(self, selection, series, episode, release_name=None):
        self.release_name = release_name
        return {'name': 'ManualImport', 'importMode': 'copy', 'files': [{'path': selection.media_path}]}

    def search(self, series_id):
        self.sent.append(('command', 'SeriesSearch'))
        return 99


class KeepTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.database = str(base / 'akon.sqlite')
        self.db = sqlite3.connect(self.database)
        initialize(self.db)
        self.addCleanup(self.db.close)
        self.configuration = Configuration(self.database)
        for folder in ('staging', 'keep'):
            (base / folder).mkdir()
        self.configuration.values.update({
            'storage.user_evaluation_root': str(base / 'staging'), 'storage.keep_root': str(base / 'keep'),
            'sonarr.base_url': 'http://sonarr.test', 'sonarr.staging_root': '', 'sonarr.library_root': '',
            'sonarr.quality_profile_id': 4, 'sonarr.timeout_seconds': 5, 'sonarr.long_running_threshold': 0,
            'sonarr.backfill_long_running': False, 'shoko.base_url': '', 'jellyfin.base_url': '',
            'acquisition.audio_check': 'off', 'qbittorrent.base_url': 'http://qb.test',
            'storage.download_root': str(base / 'downloads'), 'qbittorrent.category': 'akon',
            'qbittorrent.timeout_seconds': 5})
        staged = base / 'staging' / STAGED
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b'v' * 100)
        self.staged = staged
        wake(self.db, 18162, 'Dungeon People', now=1)
        add_torrent(self.db, 18162, {'infohash': 'a' * 40, 'release_name': '[G] Dungeon People - 05', 'step': 'done',
                                     'state': 'done', 'episodes': ['S01E05'],
                                     'identity': {'anime': {'anidb_id': 18162, 'tvdb_series_id': 438420}}}, now=1)
        series = get_series(self.db, 18162)
        series.torrents[0].update(qb={'marker': 'b' * 32, 'added': 'sent'}, files=[{
            'index': 1, 'path': 'Show/Show - 05.mkv', 'size': 100, 'episode': 'S01E05', 'anidb_episode_id': 281728,
            'tvdb_episode_id': 10348661, 'staged_file': list(signature(os.stat(staged)))}])
        series.episodes['S01E05'] = {'state': 'staged', 'torrent': 'a' * 40, 'file': STAGED}
        update_series(self.db, series, now=1)
        self.db.commit()
        self.qb = FakeQb()
        self.qb.torrent = {'state': 'uploading', 'files': (), 'priorities': {}, 'progress': {}}
        qb = self.qb
        from src.anime.series_job.context import SeriesContext
        self.context = SeriesContext(verify=None, torrents=type('T', (), {'client': lambda s, c, m: qb})(),
                                     cancelled=threading.Event(), configuration=self.configuration,
                                     secrets=mock.Mock(sonarr_api_key=lambda: 'k' * 32))
        self.sonarr = FakeSonarr(self)

    def attempt(self, now):
        with mock.patch('src.anime.sonarr.Client', self.sonarr), \
                mock.patch('src.anime.sonarr.existing_file', return_value={'relative_path': 'Dungeon People/S01E05.mkv'}):
            return asyncio.run(run_series_attempt(self.db, self.context, now=now))

    def test_keep_imports_through_sonarr_searches_once_cleans_up_and_ends_kept(self):
        keep_series_sync(self.db, 18162, now=10)
        self.db.commit()
        self.attempt(100)                                 # Series added, import sent: Sonarr is importing.
        self.assertEqual(self.sonarr.sent, [('series', None), ('command', 'ManualImport')])
        self.assertEqual(self.sonarr.release_name, '[G] Dungeon People - 05')   # Its quality and group, not the file's.
        work = get_series(self.db, 18162).work['keep']
        self.assertEqual((work['sonarr']['phase'], work['imports']['S01E05']['phase']), ('READY', 'IMPORT_UNCERTAIN'))
        self.sonarr.command = 'completed'
        self.attempt(10**13)
        series = get_series(self.db, 18162)
        self.assertEqual((series.state, series.episodes['S01E05']['state']), ('KEPT', 'kept'))
        self.assertEqual(self.sonarr.sent[-1], ('command', 'SeriesSearch'))
        self.assertEqual(self.sonarr.sent.count(('series', None)), 1)          # Added once.
        self.assertEqual(self.qb.calls, [('remove',)])                         # AKON's torrent is removed.
        self.assertFalse(self.staged.exists())                                 # The staged copy too.

    def test_sonarr_refusing_stops_keep_with_why_and_retry_resumes_it(self):
        from src.anime.sonarr import KeepReview
        keep_series_sync(self.db, 18162, now=10)
        self.db.commit()
        self.sonarr.import_payload = mock.Mock(side_effect=KeepReview())
        self.attempt(100)
        series = get_series(self.db, 18162)
        self.assertIn('KEEP_IMPORT_REVIEW', series.work['keep']['stopped'])
        self.assertEqual((series.state, series.wake_at_ms), ('ASLEEP', None))   # Waits for you.
        retry_series_sync(self.db, 18162, now=200)
        self.db.commit()
        self.assertNotIn('stopped', get_series(self.db, 18162).work['keep'])

    def test_sonarr_completing_without_a_file_stops_keep_and_retry_sends_it_again(self):
        # Owner, 2026-11-28: Sonarr could not write its library folder; it marked each import
        # completed, and Keep waited for ever without saying why.
        from src.anime.series_job.keep import IMPORT_SETTLE_MS
        keep_series_sync(self.db, 18162, now=10)
        self.db.commit()
        self.attempt(100)                                 # Import sent.
        self.sonarr.command, self.sonarr.imports = 'completed', False
        self.attempt(20_000)                              # Completed, no file: it may still appear.
        self.assertIn('completed_without_file_ms', get_series(self.db, 18162).work['keep']['imports']['S01E05'])
        self.assertNotIn('stopped', get_series(self.db, 18162).work['keep'])
        self.attempt(20_000 + IMPORT_SETTLE_MS)
        series = get_series(self.db, 18162)
        self.assertIn('IMPORT_NOT_DONE', series.work['keep']['stopped'])
        self.assertIn("Sonarr's log", series.work['keep']['stopped'])
        self.assertEqual(series.work['keep']['imports']['S01E05'], {'phase': 'PLANNED'})
        retry_series_sync(self.db, 18162, now=30_000 + IMPORT_SETTLE_MS)
        self.db.commit()
        self.sonarr.command, self.sonarr.imports = 'queued', True     # Sonarr's folder fixed.
        self.attempt(40_000 + IMPORT_SETTLE_MS)           # The import is sent again,
        self.assertEqual(self.sonarr.sent.count(('command', 'ManualImport')), 2)
        self.sonarr.command = 'completed'
        self.attempt(10**13)                              # and the series is kept.
        self.assertEqual(get_series(self.db, 18162).state, 'KEPT')

    def test_shoko_is_not_asked_for_kept_files_sonarr_has_since_replaced(self):
        # Owner, 2026-11-28: Sonarr upgraded the kept episodes minutes after the import; Shoko
        # was asked for the deleted files for 30 minutes.
        from src.anime.series_job.keep import kept_files
        keep_series_sync(self.db, 18162, now=10)
        self.db.commit()
        self.attempt(100)
        self.sonarr.command = 'completed'
        self.attempt(10**13)
        series = get_series(self.db, 18162)
        record = series.work['keep']['imports']['S01E05']
        record['result']['episode_file_id'] = 9
        self.assertEqual(kept_files(series, {55: 9}), [('Dungeon People/S01E05.mkv', 281728)])   # Still Sonarr's.
        self.assertEqual(kept_files(series, {55: 12}), [])            # Upgraded: a new file.
        self.assertEqual(kept_files(series, {55: 0}), [])             # Deleted.
        self.assertEqual(len(kept_files(series)), 1)                  # adm shoko: every imported file.


if __name__ == '__main__':
    unittest.main()
