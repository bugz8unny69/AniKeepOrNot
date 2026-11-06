"""WI-16: adm recover batch-audit previews read-only and applies only the previewed plan."""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.nextgen.job_recovery import main as recover


def main(argv, **kwargs):
    """Through adm recover, as an operator runs it."""
    if kwargs:
        from src.nextgen.batch.backlog_audit import main as audit
        return audit(argv, **kwargs)
    return recover(['batch-audit', *argv])
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

    def test_preview_then_apply(self):
        before = self.snapshot()
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn('dry run, nothing is changed', out)
        self.assertIn('qBittorrent was not checked', out)          # Reported, not silently skipped.
        self.assertIn('cancel duplicate operation', out)
        self.assertEqual(self.snapshot(), before)
        self.assertIn('adm recover batch-audit --apply', out)

        code, out, _ = self.run_cli('--apply')
        self.assertEqual(code, 0)
        self.assertIn('applied plan', out)                          # It shows what it did.
        self.assertIn('requested cleanup of 1 duplicate operations', out)
        with self.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM job_operations WHERE state='CANCEL_REQUESTED'").fetchone(),
                             (1,))
            self.assertEqual(db.execute("SELECT count(*) FROM job_operations WHERE cleanup_state='COMPLETE'").fetchone(),
                             (0,))


FILES = None


class ReadOnlyQb:
    """qBittorrent seen by the audit: login and reads only; anything else fails the test."""
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def __call__(self, options, credentials):
        self.credentials = credentials
        return self

    def login(self):
        self.calls.append('login')

    def inspect(self, digest, *, include_files=True):
        self.calls.append('inspect')
        return self.rows.get(digest)

    def __getattr__(self, name):
        raise AssertionError(f'the audit must not call qBittorrent {name}')


class AdmRoutingTests(unittest.TestCase):
    def test_the_audit_lives_under_adm_recover_only(self):
        from unittest import mock
        from src.nextgen.api import adm
        with mock.patch('src.nextgen.job_recovery.main', return_value=0) as recover, \
                mock.patch('sys.argv', ['adm', 'recover', 'batch-audit', '--apply']), \
                self.assertRaises(SystemExit) as done:
            adm.main()
        self.assertEqual(done.exception.code, 0)
        recover.assert_called_once_with(['batch-audit', '--apply'])
        with mock.patch('sys.argv', ['adm', 'batch-audit']), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as old:
            adm.main()
        self.assertEqual(old.exception.code, 2)                 # The old top-level command is gone.


class TenReleaseAuditTests(unittest.TestCase):
    """WI-17 acceptance: ten known releases; inventories and ownership uncertainties are accurate."""
    def setUp(self):
        from src.nextgen.acquisition.metadata import TorrentFile
        from src.nextgen.acquisition.submission import Observation
        from src.nextgen.secrets import SecretStore
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.database = self.root / 'q.sqlite'
        db = sqlite3.connect(self.database)
        initialize(db)
        db.close()
        ConfigurationStore(self.root).initialize({'storage': {'database_path': str(self.database),
                                                              'download_root': '/srv/downloads'},
                                                  'qbittorrent': {'base_url': 'http://qb:8080'}})
        SecretStore(self.root).initialize(qbittorrent_username='u', qbittorrent_password='p')
        files = (TorrentFile(0, 'Cyberpunk/E01.mkv', 10), TorrentFile(1, 'Cyberpunk/E02.mkv', 10))
        marker = 'c' * 32
        self.digests = [f'{n:040x}' for n in range(1, 11)]
        with sqlite3.connect(self.database) as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute("INSERT INTO identified_anime(anidb_id,tvdb_series_id,title) VALUES(16999,393400,'Cyberpunk')")
            for n, digest in enumerate(self.digests, 1):
                body = json.dumps({'release_name': f'[G{n}] Cyberpunk Edgerunners - S01 [1080p]',
                                   'torrent_infohash': digest}).encode()
                db.execute("INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms,state) "
                           "VALUES(?, 'autobrr', ?, 1, 'IDENTIFIED')", (n, body))
                db.execute("INSERT INTO identity_results(event_id,anidb_id,schema_version,series_json,validated_at_ms) "
                           "VALUES(?,16999,2,?,1)", (n, SERIES))
                job = enqueue_job(db, component='intake', kind='VERIFY_RELEASE', payload={'anidb_id': 16999},
                                  available_at_ms=0, event_id=n)
                if n in (1, 2):    # 1 is ours and matches its journal; 2 has a journal it no longer matches.
                    db.execute(
                        "INSERT INTO batch_metadata_admissions(event_id,verification_job_id,info_hash,ownership_marker,"
                        "base_url,save_path,category,timeout_seconds,api_major,state,created_at_ms,updated_at_ms) "
                        "VALUES(?,?,?,?,'http://qb:8080',?,'anime',30,5,'UNCERTAIN',1,1)",
                        (n, job, digest, marker if n == 1 else 'e' * 32,
                         f"/srv/downloads/ad-{marker if n == 1 else 'e' * 32}"))
            db.commit()
        tag = ('ad-' + marker,)
        owned_path = f'/srv/downloads/ad-{marker}'
        self.qb = ReadOnlyQb({
            self.digests[0]: Observation(self.digests[0], 'stoppedDL', owned_path, 'anime', tag, False, files),
            self.digests[1]: Observation(self.digests[1], 'stoppedDL', '/elsewhere', 'anime', tag, False, files),
            self.digests[2]: Observation(self.digests[2], 'uploading', '/media/other', 'tv', (), False, files),
            self.digests[3]: Observation(self.digests[3], 'uploading', '/media/other', 'tv', (), False, files),
            self.digests[4]: Observation(self.digests[4], 'metaDL', '/media/other', 'tv', (), False, ()),
            self.digests[5]: Observation(self.digests[5], 'stoppedDL', '/x', 'anime', ('ad-' + 'd' * 32,), False, files),
        })

    def test_ten_releases_are_classified_accurately_without_changing_anything(self):
        before = self.database.read_bytes()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(['--json', '--config-dir', str(self.root)], qb_client_factory=self.qb)
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertIsNone(report['qbittorrent_note'])                 # It really logged in and read.
        self.assertEqual(self.qb.credentials.username, 'u')
        self.assertEqual(set(self.qb.calls), {'login', 'inspect'})
        scope, = report['scopes']
        self.assertEqual(scope['summary']['known_candidates'], 10)
        ownership = {c['info_hash']: c['ownership'] for c in scope['register_candidates']}
        d = self.digests
        self.assertEqual(ownership[d[0]], 'OWNED')
        self.assertEqual(ownership[d[1]], 'UNCERTAIN')                # Journal says ours; qB disagrees.
        self.assertEqual((ownership[d[2]], ownership[d[3]], ownership[d[4]]), ('FOREIGN',) * 3)
        self.assertEqual(ownership[d[5]], 'UNCERTAIN')                # Someone's ad- tag, no journal.
        self.assertEqual({ownership[x] for x in d[6:]}, {'NOT_IN_QBITTORRENT'})
        ready = {c['info_hash'] for c in scope['register_candidates'] if c['has_metadata']}
        self.assertEqual(ready, {d[0], d[1], d[2], d[3], d[5]})     # Complete file lists only; d[4] resolving.
        self.assertEqual(scope['summary']['metadata_ready'], 5)
        self.assertEqual(scope['summary']['ownership'],
                         {'OWNED': 1, 'UNCERTAIN': 2, 'FOREIGN': 3, 'NOT_IN_QBITTORRENT': 4})
        self.assertEqual(scope['summary']['competing_operations'], 10)
        self.assertEqual(len(scope['cancel_operations']), 9)
        self.assertEqual(self.database.read_bytes(), before)        # Preview wrote nothing.

    def test_an_unreachable_qbittorrent_is_reported(self):
        from src.nextgen.acquisition.submission import ClientUnavailable

        class Down(ReadOnlyQb):
            def login(self):
                raise ClientUnavailable()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(['--config-dir', str(self.root)], qb_client_factory=Down({}))
        self.assertIn('qBittorrent was not checked (ClientUnavailable', out.getvalue())
        self.assertIn('10 known candidate(s), 0 with a file list', out.getvalue())


if __name__ == '__main__':
    unittest.main()
