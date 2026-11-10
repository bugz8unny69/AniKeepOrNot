"""Batch reuse of real staging receipts, atomic replacement and crash recovery."""
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from src.anime import transcode_batch as batch
from src.anime.services.transcode import TranscodeWaiting, TranscodeFailed
from src.anime.storage.sqlite import connect, transaction
from src.anime.tests import test_stage_media_job as staging_fixture


class TranscodeBatchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = staging_fixture.StageMediaJobTests.asyncSetUp
    rows = staging_fixture.StageMediaJobTests.rows
    source = staging_fixture.StageMediaJobTests.source
    queued = staging_fixture.StageMediaJobTests.queued
    due = staging_fixture.StageMediaJobTests.due
    ready = staging_fixture.StageMediaJobTests.ready
    worker = staging_fixture.StageMediaJobTests.worker
    staged_ready = staging_fixture.StageMediaJobTests.staged_ready

    async def published(self):
        worker = self.worker()
        target, original = await self.staged_ready(worker)
        await worker.attempt()
        self.store.update({'transcode': {'mode':'local', 'target_mb':1, 'tmp':str(self.root/'temp')}}, expected_revision=4)
        return next(target.rglob('*.mkv'))

    def queue(self):
        with connect(self.database) as db, transaction(db): return batch.enqueue(db)

    async def test_dry_run_and_under_target_are_read_only(self):
        path = await self.published()
        configuration = self.store.read()
        with connect(self.database) as db:
            self.assertEqual(batch.dry_run(db, configuration), [])
            self.assertEqual(db.execute('SELECT count(*) FROM transcode_batches').fetchone()[0], 0)
        self.queue()
        self.assertTrue(batch.run_batch(configuration, None, threading.Event()))
        self.assertEqual(self.rows('SELECT done,skipped,failed,state FROM transcode_batches'), [(0,1,0,'DONE')])
        self.assertTrue(path.exists())

    async def test_atomic_replacement_updates_records_and_notifies_once(self):
        path = await self.published()
        # The fixture is intentionally tiny; use a correspondingly tiny budget.
        self.queue()
        calls = []
        def client(source, output, config, secrets, stop, **kwargs):
            kwargs['started']()
            calls.append(source)
            Path(output).write_bytes(b'shrunk')
            return True
        with patch.object(batch, 'target_bytes', return_value=1):
            batch.run_batch(self.store.read(), None, threading.Event(), client=client)
        self.assertEqual(path.read_bytes(), b'shrunk')
        self.assertEqual(self.rows('SELECT done,skipped,failed,state FROM transcode_batches'), [(1,0,0,'DONE')])
        plan, receipt = self.rows('SELECT manifest_json,receipts_json FROM staging_publications')[0]
        self.assertEqual(next(e for e in json.loads(plan)['entries'] if e['role']=='media')['size'], 6)
        saved = next(e for e in json.loads(receipt) if e['role']=='media')
        self.assertEqual(saved['file'][2:4], [6,path.stat().st_mtime_ns])
        self.assertEqual(self.rows("SELECT size FROM resources WHERE kind='STAGED_FILE' AND path=?", (str(path),)), [(6,)])
        batch.run_batch(self.store.read(), None, threading.Event(), client=client)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.rows("SELECT count(*) FROM notifications WHERE type='TRANSCODE_BATCH_DONE'"), [(1,)])

    async def test_crash_after_rename_resumes_without_second_encode(self):
        path = await self.published()
        self.queue()
        calls = []
        def client(source, output, *args, **kwargs):
            kwargs['started'](); calls.append(1); Path(output).write_bytes(b'encoded'); return True
        real_catalog = batch._catalog
        class Crash(BaseException): pass
        with patch.object(batch, 'target_bytes', return_value=1), patch.object(batch, '_catalog', side_effect=Crash()):
            with self.assertRaises(Crash): batch.run_batch(self.store.read(), None, threading.Event(), client=client)
        self.assertEqual(path.read_bytes(), b'encoded')
        with patch.object(batch, '_catalog', real_catalog):
            batch.run_batch(self.store.read(), None, threading.Event(), client=client)
        self.assertEqual(calls, [1])
        self.assertEqual(self.rows('SELECT done,state FROM transcode_batches'), [(1,'DONE')])

    async def test_busy_batch_refused_and_failures_listed(self):
        await self.published()
        self.queue()
        with self.assertRaises(TranscodeWaiting): self.queue()
        def client(source, output, *args, **kwargs):
            for _ in range(3): kwargs['started']()
            raise TranscodeFailed('three attempts failed')
        with patch.object(batch, 'target_bytes', return_value=1):
            batch.run_batch(self.store.read(), None, threading.Event(), client=client)
        failed, names = self.rows('SELECT failed,failed_files FROM transcode_batches')[0]
        self.assertEqual(failed, 1)
        self.assertIn('three attempts failed', names)
        self.assertEqual(self.rows('SELECT json_extract(progress_json,\'$[0].attempts\') FROM transcode_batches'), [(3,)])

    async def test_mixed_batch_resumes_and_counts_each_file_once(self):
        path = await self.published()
        with connect(self.database) as db: original = batch.staged_files(db)[0]
        files = []
        for index, size in enumerate((100,100,10)):
            current = path.with_name(f'episode-{index}.mkv')
            current.write_bytes(b'x'*size)
            from src.anime.acquisition.media import signature
            files.append({**original, 'relative':str(current.relative_to(original['root'])),
                          'file':list(signature(current.stat()))})
        with patch.object(batch, 'staged_files', return_value=files): self.queue()
        calls = []
        def client(source, output, *args, **kwargs):
            kwargs['started'](); calls.append(source.name); Path(output).write_bytes(b'done'); return True
        with patch.object(batch, 'target_bytes', return_value=50), patch.object(batch, '_catalog'):
            for _ in range(4): batch.run_batch(self.store.read(), None, threading.Event(), client=client)
        self.assertEqual(calls, ['episode-0.mkv','episode-1.mkv'])
        self.assertEqual(self.rows('SELECT done,skipped,failed,state FROM transcode_batches'), [(2,1,0,'DONE')])

    async def test_completion_notification_obeys_existing_event_contract(self):
        from src.anime import notifications
        from src.anime.tests import test_notifications as notification_fixture
        await self.published()
        self.queue()
        recorder = notification_fixture.Recorder()
        with patch.object(notifications, 'CHANNELS', (recorder,)):
            batch.run_batch(self.store.read(), None, threading.Event())
        self.assertEqual(len(recorder.events), 1)
        notification_fixture.check_contract(self, recorder.events[0])
        self.assertEqual(notifications.EVENT_TYPES[recorder.events[0]['type']], ('open_jobs',))

    async def test_cli_queues_and_exits_dry_run_changes_nothing(self):
        import contextlib
        import io
        from src.anime.transcode_cli import main
        await self.published()
        args = ['--config-dir',str(self.root)]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([*args,'staged','--dry-run']),0)
        self.assertIn('estimated saving',output.getvalue())
        self.assertEqual(self.rows('SELECT count(*) FROM transcode_batches'),[(0,)])
        with patch('builtins.input',return_value='n'):
            self.assertEqual(main([*args,'staged']),0)
        self.assertEqual(self.rows('SELECT count(*) FROM transcode_batches'),[(0,)])
        jobs = self.rows('SELECT count(*) FROM jobs')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([*args,'staged','--yes']),0)
            self.assertEqual(main([*args,'status']),0)
        self.assertEqual(self.rows('SELECT state FROM transcode_batches'),[('QUEUED',)])
        self.assertEqual(self.rows('SELECT count(*) FROM jobs'),jobs)

    async def test_decision_during_encode_preserves_the_staged_original(self):
        path = await self.published()
        original = path.read_bytes()
        self.queue()
        def client(source, output, *args, **kwargs):
            kwargs['started']()
            Path(output).write_bytes(b'new')
            with connect(self.database) as db, transaction(db):
                job, operation = db.execute('SELECT s.job_id,m.operation_id FROM staging_publications s JOIN job_operation_members m USING(job_id)').fetchone()
                db.execute("INSERT INTO anime_review_decisions(anidb_id,operation_id,job_id,decision,cleanup_state,decided_at_ms) VALUES(123,?,?,'ACCEPT','PENDING',1)", (operation,job))
            return True
        with patch.object(batch,'target_bytes',return_value=1):
            batch.run_batch(self.store.read(),None,threading.Event(),client=client)
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(self.rows('SELECT failed FROM transcode_batches'),[(1,)])
