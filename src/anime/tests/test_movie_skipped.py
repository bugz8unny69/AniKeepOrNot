"""A movie release is canceled with a notification, never left in Needs review."""
import tempfile
import unittest
from pathlib import Path

from src.anime.configuration import ConfigurationStore
from src.anime.job_contracts import Registry, Policy
from src.anime.jobs.verify_release_job import registration
from src.anime.storage.sqlite import accept_event_and_job, connect, initialize, save_identity
from src.anime.worker import Worker


class MovieSkippedTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, release, anime_type):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.database = root / 'anime.sqlite'
        store = ConfigurationStore(root)
        startup = store.initialize({'storage': {'database_path': str(self.database)}}).snapshot
        db = connect(self.database)
        initialize(db)
        event, self.job = accept_event_and_job(
            db, 'test', ('{"release_name":"%s","torrent_infohash":"%s"}' % (release, 'a' * 40)).encode(),
            kind='VERIFY_RELEASE', payload={'anidb_id': 123})
        save_identity(db, event, {'schema_version': 2, 'anime': {
            'anidb_id': 123, 'tvdb_series_id': 456, 'title': 'Suzume no Tojimari', 'type': anime_type}})
        db.close()
        self.verified = []
        async def verify(source, *args, **kwargs):
            self.verified.append(source)
            raise AssertionError('a movie must not be verified')
        self.worker = Worker(store, startup, Registry((registration(verify),)), policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(self.worker.close)
        await self.worker.attempt()

    def rows(self, sql, *args):
        db = connect(self.database)
        try: return db.execute(sql, args).fetchall()
        finally: db.close()

    def assert_skipped(self):
        self.assertEqual(self.verified, [])
        self.assertEqual(self.rows('SELECT state,last_reason_code FROM jobs WHERE job_id=?', self.job),
                         [('CANCELED', 'RELEASE_MOVIE_SKIPPED')])
        self.assertEqual(self.rows('SELECT o.state,o.cleanup_state FROM job_operations o JOIN job_operation_members m '
                                   'USING(operation_id) WHERE m.job_id=?', self.job), [('CANCELED', 'COMPLETE')])
        self.assertEqual(self.rows('SELECT type,job_id,anidb_id FROM notifications'),
                         [('MOVIE_SKIPPED', self.job, 123)])

    async def test_anidb_movie_type_is_skipped(self):
        await self.start('[Group] Suzume (2022) [BD 1080p] [Dual Audio]', 'Movie')
        self.assert_skipped()

    async def test_movie_word_in_title_is_skipped(self):
        await self.start('[Group] Gekijouban Suzume no Tojimari (BD 1080p) [Dual Audio]', None)
        self.assert_skipped()

    async def test_season_pack_with_a_movie_is_not_skipped(self):
        await self.start('[Group] Shiki S01 + Movie [BD 1080p] [Dual Audio]', 'TV Series')
        self.assertEqual(len(self.verified), 1)
        self.assertEqual(self.rows('SELECT count(*) FROM notifications'), [(0,)])


if __name__ == '__main__':
    unittest.main()
