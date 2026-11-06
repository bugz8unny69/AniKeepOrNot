"""WI-16: adm batch-audit previews read-only and applies only the previewed plan."""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.nextgen.batch.backlog_audit import main
from src.nextgen.configuration import ConfigurationStore
from src.nextgen.storage.sqlite import enqueue_job, initialize, transaction

SERIES = json.dumps({'schema_version': 2, 'anime': {'anidb_id': 16999, 'tvdb_series_id': 393400,
                                                    'title': 'Cyberpunk Edgerunners'}, 'episodes': []})


class BatchAuditCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.database = self.root / 'q.sqlite'
        db = sqlite3.connect(self.database)
        initialize(db)
        db.close()
        ConfigurationStore(self.root).initialize({'storage': {'database_path': str(self.database)}})
        with self.connect() as db, transaction(db):
            db.execute("INSERT INTO identified_anime(anidb_id,tvdb_series_id,title) VALUES(16999,393400,'Cyberpunk')")
            for event, digest in ((1, '1' * 40), (2, '2' * 40)):
                body = json.dumps({'release_name': f'[G{event}] Cyberpunk Edgerunners - S01 [1080p]',
                                   'torrent_infohash': digest}).encode()
                db.execute("INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms,state) "
                           "VALUES(?, 'autobrr', ?, 1, 'IDENTIFIED')", (event, body))
                db.execute("INSERT INTO identity_results(event_id,anidb_id,schema_version,series_json,validated_at_ms) "
                           "VALUES(?,16999,2,?,1)", (event, SERIES))
                enqueue_job(db, component='intake', kind='VERIFY_RELEASE', payload={'anidb_id': 16999},
                            available_at_ms=0, event_id=event)

    def connect(self):
        db = sqlite3.connect(self.database)
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main([*args, '--config-dir', str(self.root)])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def snapshot(self):
        with self.connect() as db:
            return (db.execute("SELECT * FROM jobs ORDER BY job_id").fetchall(),
                    db.execute("SELECT * FROM job_operations ORDER BY operation_id").fetchall(),
                    db.execute("SELECT count(*) FROM batch_candidates").fetchall())

    def test_preview_then_apply_exactly_that_plan(self):
        before = self.snapshot()
        code, out, _ = self.run_cli('preview')
        self.assertEqual(code, 0)
        self.assertIn('dry run: nothing is changed', out)
        self.assertIn('qBittorrent was not checked', out)          # Reported, not silently skipped.
        self.assertIn('cancel duplicate operation', out)
        self.assertEqual(self.snapshot(), before)
        token = out.split('--plan-token ')[1].split()[0]

        code, _, err = self.run_cli('apply')
        self.assertEqual(code, 2)                                   # A token is required.
        code, _, err = self.run_cli('apply', '--plan-token', 'stale')
        self.assertEqual(code, 1)
        self.assertIn('plan changed', err)
        self.assertEqual(self.snapshot(), before)

        code, out, _ = self.run_cli('apply', '--plan-token', token)
        self.assertEqual(code, 0)
        self.assertIn('requested cleanup of 1 duplicate operations', out)
        with self.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM job_operations WHERE state='CANCEL_REQUESTED'").fetchone(),
                             (1,))
            self.assertEqual(db.execute("SELECT count(*) FROM job_operations WHERE cleanup_state='COMPLETE'").fetchone(),
                             (0,))


if __name__ == '__main__':
    unittest.main()
