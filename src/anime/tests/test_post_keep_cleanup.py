"""Offline selected staging cleanup and unchanged permanent import; unrun."""
from functools import partial
from pathlib import Path
import unittest

from src.anime.post_keep_cleanup import Service, registration
from src.anime.job_contracts import Registry
from src.anime.secrets import SecretStore
from src.anime.sonarr import Client, KeepUnavailable
from src.anime.storage.review_decisions import remove_selected
from src.anime.tests.test_keep_sonarr import KeepSonarrTests
from src.anime.tests.test_stage_media_job import StageMediaJobTests
from src.anime.tests.test_verify_media_job import VerifyMediaJobTests
from src.anime.tests.test_torrent_transfer import TorrentTransferTests
from src.anime.tests.test_acquisition_preparation import AcquisitionPreparationTests


class PostKeepCleanupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=AcquisitionPreparationTests.asyncSetUp
    rows=AcquisitionPreparationTests.rows
    source=AcquisitionPreparationTests.source
    queued=TorrentTransferTests.queued
    due=TorrentTransferTests.due
    ready=VerifyMediaJobTests.ready
    staged_ready=StageMediaJobTests.staged_ready
    prepared=KeepSonarrTests.prepared
    make_due=KeepSonarrTests.make_due

    async def cleanup_ready(self,*,remove=remove_selected):
        worker,fake,actions,target,keep=await self.prepared()
        factory=partial(Service,client_factory=lambda options,key:Client(options,key,transport=fake),remove=remove)
        worker.registry=Registry((*worker.registry.entries.values(),registration(SecretStore(self.root),service_factory=factory)))
        await worker.attempt()
        cleanup=self.rows("SELECT job_id FROM jobs WHERE kind='CLEANUP_KEPT_MEDIA'")[0][0]
        return worker,fake,actions,target,keep,cleanup

    async def test_removes_only_selected_staged_media_and_keeps_series_and_permanent(self):
        worker,fake,actions,target,keep,cleanup=await self.cleanup_ready()
        stage=Path(fake.selection.staging_root)
        unrelated=stage/'other.mkv';unrelated.write_bytes(b'unrelated')
        nfos=list(stage.rglob('*.nfo'));self.assertEqual(len(nfos),2)
        permanent=fake.root/'Series'/'episode.mkv';before=permanent.read_bytes()
        await worker.attempt()
        self.assertFalse(Path(fake.selection.media_path).exists())
        self.assertEqual(permanent.read_bytes(),before)
        self.assertEqual(len(list(stage.rglob('*.nfo'))),1)
        self.assertTrue(unrelated.is_file())
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(cleanup,)),[('DONE',)])
        _,receipt,_=actions.submit(target,'keep')
        self.assertTrue(receipt['post_keep_cleanup']['completed'])
        # Jellyfin is unconfigured in this fixture, so neither step queues a scan.
        self.assertEqual(self.rows(
            "SELECT count(*) FROM jobs WHERE kind='REFRESH_JELLYFIN'"),[(0,)])
        self.assertEqual(self.rows('SELECT count(*) FROM episode_reservations'),[(1,)])
        self.assertEqual(fake.posts.count('command'),1)
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='CLEANUP_KEPT_MEDIA'"),[(1,)])

    async def test_missing_permanent_preserves_staging_and_automatic_retry_does_not_reimport(self):
        worker,fake,actions,target,keep,cleanup=await self.cleanup_ready()
        permanent=fake.root/'Series'/'episode.mkv';saved=permanent.read_bytes();permanent.unlink()
        await worker.attempt()
        self.assertTrue(Path(fake.selection.media_path).is_file())
        self.assertEqual(self.rows('SELECT state,last_reason_code FROM jobs WHERE job_id=?',(cleanup,)),[('RETRY_WAIT','POST_KEEP_CLEANUP_UNAVAILABLE')])
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(keep,)),[('DONE',)])
        permanent.write_bytes(saved)
        await self.make_due(cleanup)
        await worker.attempt()
        self.assertFalse(Path(fake.selection.media_path).exists())
        self.assertEqual(fake.posts.count('command'),1)

    async def test_interrupted_delete_can_resume_with_missing_selected_files(self):
        calls=[]
        def interrupted(manifest,receipts,guard,root):
            remove_selected(manifest,receipts,guard,root)
            calls.append(True)
            if len(calls)==1:raise OSError('response lost after unlink')
        worker,fake,actions,target,keep,cleanup=await self.cleanup_ready(remove=interrupted)
        await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(cleanup,)),[('RETRY_WAIT',)])
        await self.make_due(cleanup);await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(cleanup,)),[('DONE',)])
        self.assertEqual(fake.posts.count('command'),1)

    async def test_replaced_staged_file_is_left_for_review(self):
        worker,fake,actions,target,keep,cleanup=await self.cleanup_ready()
        path=Path(fake.selection.media_path);path.unlink();path.write_bytes(b'changed')
        await worker.attempt()
        self.assertEqual(path.read_bytes(),b'changed')
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(cleanup,)),[('NEEDS_REVIEW',)])

    async def test_sonarr_unavailable_does_not_undo_keep(self):
        worker,fake,actions,target,keep,cleanup=await self.cleanup_ready()
        def unavailable(*args):raise KeepUnavailable()
        factory=partial(Service,client_factory=unavailable)
        entries=tuple(r for r in worker.registry.entries.values() if r.kind!='CLEANUP_KEPT_MEDIA')
        worker.registry=Registry((*entries,registration(SecretStore(self.root),service_factory=factory)))
        await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(cleanup,)),[('RETRY_WAIT',)])
        self.assertTrue(Path(fake.selection.media_path).is_file())
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(keep,)),[('DONE',)])

    async def test_direct_review_retry_resumes_only_cleanup_and_keeps_the_import(self):
        from src.anime.storage.review_retry import request_review_retry, capability
        from src.anime.storage.sqlite import connect, transaction
        worker,fake,actions,target,keep,cleanup = await self.cleanup_ready()
        permanent = fake.root/'Series'/'episode.mkv'
        saved = permanent.read_bytes()
        permanent.unlink()
        await worker.attempt()
        with connect(self.database) as db, transaction(db):
            db.execute("UPDATE jobs SET state='NEEDS_REVIEW' WHERE job_id=?",(cleanup,))
            attempt,reason = db.execute('SELECT attempt_count,last_reason_code FROM jobs WHERE job_id=?', (cleanup,)).fetchone()
            self.assertTrue(capability(db,cleanup)['enabled'])
        permanent.write_bytes(saved)
        receipt = request_review_retry(self.database,cleanup,expected_attempt=attempt,expected_reason=reason)
        self.assertEqual(receipt.job_id,cleanup)
        await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(cleanup,)),[('DONE',)])
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(keep,)),[('DONE',)])
        self.assertEqual(fake.posts.count('command'),1)
        self.assertEqual(permanent.read_bytes(),saved)
        self.assertFalse(Path(fake.selection.media_path).exists())
