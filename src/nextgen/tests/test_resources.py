"""resources (JD-1c): one record of every file, folder and torrent a job creates."""
import sqlite3
import unittest

from src.nextgen.resources import ResourceInvalid, Resources, of_job, operations, report
from src.nextgen.storage.sqlite import enqueue_job, initialize, transaction

HASH = 'a' * 40


class ResourcesTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        initialize(self.db)
        self.db.execute("INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES('autobrr',x'7b7d',1)")
        self.db.commit()
        with transaction(self.db):
            self.job = enqueue_job(self.db, kind='SUBMIT_TORRENT', payload={}, event_id=1, component='worker')

    def record(self, now=10):
        return Resources(self.db, self.job, now)

    def test_a_torrent_its_files_and_staging_are_one_chain(self):
        with transaction(self.db):
            r = self.record()
            torrent = r.torrent(HASH, save_path='/downloads/ad-x/')
            video = r.downloaded(torrent, '/downloads/ad-x/Show - 01.mkv', 100)
            staged = r.staged('/staging/Show/Season 01/Show - S01E01.mkv', 100, source_id=video)
        kinds = [(x.kind, x.path, x.parent_id, x.state) for x in of_job(self.db, self.job)]
        self.assertEqual(kinds, [('TORRENT', None, None, 'PRESENT'),
                                 ('FOLDER', '/downloads/ad-x', torrent, 'PRESENT'),
                                 ('DOWNLOADED_FILE', '/downloads/ad-x/Show - 01.mkv', torrent, 'PRESENT'),
                                 ('STAGED_FILE', '/staging/Show/Season 01/Show - S01E01.mkv', video, 'PRESENT')])
        self.assertEqual([o.operation for o in operations(self.db, staged)], ['STAGED'])

    def test_recording_again_is_a_no_op(self):
        with transaction(self.db):
            r = self.record()
            first = r.torrent(HASH)
            self.assertEqual(r.torrent(HASH), first)
            self.assertEqual(r.downloaded(first, '/d/a.mkv', 1), r.downloaded(first, '/d/a.mkv', 1))
        self.assertEqual(self.db.execute('SELECT count(*) FROM resource_operations').fetchone()[0], 2)

    def test_removing_a_torrent_with_its_data_marks_its_downloads_gone(self):
        with transaction(self.db):
            r = self.record()
            torrent = r.torrent(HASH, save_path='/downloads/ad-x')
            r.downloaded(torrent, '/downloads/ad-x/a.mkv', 1)
            staged = r.staged('/staging/a.mkv', 1)
        with transaction(self.db):
            self.assertEqual(Resources(self.db, self.job, 20).deleted_torrent(HASH), 3)
            self.assertEqual(Resources(self.db, self.job, 21).deleted_torrent(HASH), 0)   # already gone
        states = {x.kind: x.state for x in of_job(self.db, self.job)}
        self.assertEqual(states, {'TORRENT': 'DELETED', 'FOLDER': 'DELETED', 'DOWNLOADED_FILE': 'DELETED',
                                  'STAGED_FILE': 'PRESENT'})                # staging is not the torrent's data
        self.assertEqual([o.operation for o in operations(self.db, staged)], ['STAGED'])

    def test_a_library_copy_is_handed_over(self):
        with transaction(self.db):
            library = self.record().library('/library/Show/Season 01/Show - S01E01.mkv', 5)
        self.assertEqual(of_job(self.db, self.job)[0].state, 'HANDED_OVER')
        self.assertEqual([o.operation for o in operations(self.db, library)], ['IMPORTED', 'HANDED_OVER'])

    def test_invalid_records_are_refused(self):
        with transaction(self.db):
            r = self.record()
            for bad in (lambda: r.torrent('A' * 40), lambda: r.torrent('a' * 39),
                        lambda: r.staged('relative/a.mkv', 1), lambda: r.staged('/a/../b.mkv', 1),
                        lambda: r.staged('/a//b.mkv', 1), lambda: r.staged('/', 1),
                        lambda: r.staged('/a.mkv', -1)):
                with self.subTest(bad=bad), self.assertRaises(ResourceInvalid):
                    bad()

    def test_records_are_written_only_inside_a_transaction(self):
        with self.assertRaises(ValueError):
            Resources(self.db, self.job, 1)

    def test_the_schema_refuses_a_torrent_without_a_hash(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO resources(job_id,kind,state,created_at_ms,updated_at_ms) "
                            "VALUES(?,'TORRENT','PRESENT',1,1)", (self.job,))

    def test_report_shows_the_work_done_on_each_file(self):
        with transaction(self.db):
            r = self.record()
            torrent = r.torrent(HASH)
            r.downloaded(torrent, '/d/a.mkv', 7)
        with transaction(self.db):
            Resources(self.db, self.job, 30).deleted_torrent(HASH)
        self.assertEqual(report(self.db, [self.job]), [
            f'Job #{self.job:<6} torrent: {HASH} · added → deleted',
            f'Job #{self.job:<6} downloaded file: /d/a.mkv (7 bytes) · downloaded → deleted'])


class RecordedOnARealRunTests(unittest.IsolatedAsyncioTestCase):
    """The single-episode path records its torrent, download and staged files as it commits them."""
    from src.nextgen.tests.test_stage_media_job import StageMediaJobTests as _Stage
    asyncSetUp = _Stage.asyncSetUp
    asyncTearDown = getattr(_Stage, 'asyncTearDown', unittest.IsolatedAsyncioTestCase.asyncTearDown)

    async def test_staging_run_records_the_chain(self):
        stage = self._Stage.test_verified_copy_metadata_and_done_commit_with_staged_milestone
        for name in dir(self._Stage):                     # helpers the staging test uses
            if not name.startswith('test_') and not hasattr(self, name):
                setattr(self, name, getattr(self._Stage, name).__get__(self))
        await stage(self)
        rows = self.rows('SELECT kind,parent_id,state FROM resources ORDER BY resource_id')
        kinds = [k for k, _, _ in rows]
        self.assertEqual(kinds[:3], ['TORRENT', 'FOLDER', 'DOWNLOADED_FILE'])
        self.assertEqual(kinds.count('STAGED_FILE'), 3)               # video, episode NFO, tvshow.nfo
        video = self.rows("SELECT s.parent_id,d.kind FROM resources s JOIN resources d ON d.resource_id=s.parent_id "
                          "WHERE s.kind='STAGED_FILE' AND s.path LIKE '%.mkv'")
        self.assertEqual(video[0][1], 'DOWNLOADED_FILE')             # the staged video links to its download
        self.assertTrue(all(state == 'PRESENT' for _, _, state in rows))


if __name__ == '__main__':
    unittest.main()


class ResourcesMigrationTests(unittest.TestCase):
    def test_v65_upgrades_to_v66_and_the_guard_refuses_other_versions(self):
        from src.nextgen.storage.migrations import SCRIPTS, SQL_DIR
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        for name in SCRIPTS[:65]:
            db.executescript((SQL_DIR / name).read_text())
        script = (SQL_DIR / '066_resources.sql').read_text()
        db.executescript(script)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 66)
        self.assertEqual(db.execute('SELECT count(*) FROM resources').fetchone()[0], 0)
        with self.assertRaises(sqlite3.DatabaseError):
            db.executescript(script)                      # already v66: the guard rolls back
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 66)
