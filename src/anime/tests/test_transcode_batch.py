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


class TranscodeBatchLaneTests(unittest.IsolatedAsyncioTestCase):
    """Per-agent loops. Shares the fixtures only, so the tests above do not run twice."""
    for _name in ('asyncSetUp', 'rows', 'source', 'queued', 'due', 'ready', 'worker', 'staged_ready', 'published', 'queue'):
        locals()[_name] = getattr(TranscodeBatchTests, _name)
    del _name

    async def queue_sizes(self, sizes):
        path = await self.published()
        self.store.update({'transcode': {'mode': 'remote', 'target_mb': 1, 'retries': 0,
                           'agents': ['http://first:8787', 'http://second:8787']}}, expected_revision=5)
        from src.anime.acquisition.media import signature
        with connect(self.database) as db: original = batch.staged_files(db)[0]
        files = []
        for index, size in enumerate(sizes):
            current = path.with_name(f'ep-{index}.mkv')
            current.write_bytes(b'x' * size)
            files.append({**original, 'relative': str(current.relative_to(original['root'])),
                          'file': list(signature(current.stat()))})
        with patch.object(batch, 'staged_files', return_value=files): self.queue()

    async def test_a_free_agent_takes_the_next_file_while_the_other_is_still_encoding(self):
        await self.queue_sizes((100, 10, 100, 100, 10, 100))      # 10-byte files are under the target.
        long_running, order, running, most = threading.Event(), [], [0], [0]
        lock = threading.Lock()
        def client(source, output, *args, **kwargs):
            with lock:
                running[0] += 1; most[0] = max(most[0], running[0])
            kwargs['started']()
            if source.name == 'ep-0.mkv':
                assert long_running.wait(5), 'the other agent never finished the rest'
            Path(output).write_bytes(b'done')
            with lock:
                running[0] -= 1; order.append(source.name)
                if len(order) == 3: long_running.set()           # ep-2, ep-3, ep-5 done meanwhile.
            return True
        with patch.object(batch, 'target_bytes', return_value=50), patch.object(batch, '_catalog'):
            batch.run_batch(self.store.read(), None, threading.Event(), client=client)
        self.assertEqual(most[0], 2)
        self.assertEqual(order[-1], 'ep-0.mkv')
        self.assertEqual(sorted(order), ['ep-0.mkv', 'ep-2.mkv', 'ep-3.mkv', 'ep-5.mkv'])
        self.assertEqual(self.rows('SELECT done,skipped,failed,state FROM transcode_batches'), [(4, 2, 0, 'DONE')])

    async def test_a_file_no_agent_takes_waits_for_the_busy_one_and_the_batch_still_finishes(self):
        await self.queue_sizes((100, 100, 100))
        busy, done = threading.Lock(), []
        def client(source, output, *args, **kwargs):
            if not busy.acquire(blocking=False):                  # Only one agent will take work.
                raise TranscodeWaiting('Waiting for transcoder')
            try:
                kwargs['started'](); Path(output).write_bytes(b'done'); done.append(source.name); return True
            finally: busy.release()
        with patch.object(batch, 'target_bytes', return_value=50), patch.object(batch, '_catalog'), \
                patch.object(batch, '_lanes', side_effect=lambda *a, **k: real_lanes(*a, **{**k, 'retry_s': 0.01})):
            for _ in range(5):
                batch.run_batch(self.store.read(), None, threading.Event(), client=client)
        self.assertEqual(sorted(done), ['ep-0.mkv', 'ep-1.mkv', 'ep-2.mkv'])
        self.assertEqual(self.rows('SELECT done,failed,state FROM transcode_batches'), [(3, 0, 'DONE')])


real_lanes = batch._lanes
