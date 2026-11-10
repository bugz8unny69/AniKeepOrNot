"""Verified input -> shared transcode -> existing Publisher and cleanup handoff."""
from functools import partial
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from src.anime.acquisition.media import verify_file
from src.anime.jobs.verify_media_job import registration as verify_registration
from src.anime.jobs.stage_user_evaluation_media_job import registration as stage_registration
from src.anime.jobs.transcode_media_job import registration as transcode_registration
from src.anime.services import transcode as client
from src.anime.staging import Publisher
from src.anime.tests import test_stage_media_job as fixture
from src.anime.tests import test_torrent_transfer as transfer_fixture
from src.anime.tests.test_media_verification import probe_bytes
from src.anime.tests.test_transcode_client import Secrets


class TranscodePipelineTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.StageMediaJobTests.asyncSetUp
    rows = fixture.StageMediaJobTests.rows
    source = fixture.StageMediaJobTests.source
    queued = fixture.StageMediaJobTests.queued
    due = fixture.StageMediaJobTests.due
    ready = fixture.StageMediaJobTests.ready
    staged_ready = fixture.StageMediaJobTests.staged_ready

    def worker(self):
        verify = verify_registration(verify=partial(verify_file, read_probe=lambda *_: probe_bytes()),
                                    staging_kind='STAGE_USER_EVALUATION_MEDIA', transcode_kind='TRANSCODE_MEDIA')
        stage = stage_registration(publisher_factory=partial(Publisher, allow_root=True), secrets=Secrets())
        return transfer_fixture.TorrentTransferTests.worker(self, verification=verify,
                        additional=(transcode_registration(Secrets()), stage))

    async def test_off_routes_directly_to_staging(self):
        worker = self.worker()
        await self.staged_ready(worker)
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='TRANSCODE_MEDIA'"), [(0,)])
        await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM staging_publications'), [('PUBLISHED',)])

    async def test_remote_result_staged_with_original_torrent_evidence_preserved(self):
        worker = self.worker()
        _, _, source = await self.ready(worker)
        staging = self.root/'staging'; staging.mkdir()
        self.store.update({'storage': {'user_evaluation_root':str(staging)},
                           'transcode': {'mode':'remote','agents':['agent:8787'],'retries':0}}, expected_revision=3)
        await worker.attempt()  # verification routes into the new step
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='TRANSCODE_MEDIA'"), [('PENDING',)])
        original = source.read_bytes()
        def run(remote, source, output, rate, audio, flag, report, started):
            started(); output.write_bytes(b'smaller-dual-audio'); report('Receiving result')
        with patch.object(client.Remote, 'health', return_value={'busy':False,'free_bytes':10000000}), \
             patch.object(client.Remote, 'run', run), patch.object(client.encode, 'check'), \
             patch.object(client, 'target_bytes', return_value=1), patch.object(client, 'video_kbps', return_value=100):
            await worker.attempt()
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='TRANSCODE_MEDIA'"), [('DONE',)])
        await worker.attempt()
        self.assertEqual(self.rows('SELECT state FROM staging_publications'), [('PUBLISHED',)])
        self.assertEqual(next(staging.rglob('*.mkv')).read_bytes(), b'smaller-dual-audio')
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(self.rows("SELECT json_extract(manifest_json,'$.transfer') FROM staging_publications"), [('MOVE',)])
        self.assertEqual(list((staging/'.transcode').glob('*.mkv')),[])
        self.assertEqual(self.rows('SELECT attempts FROM transcode_media'), [(1,)])
        self.assertEqual(self.rows('SELECT count(*) FROM media_probe_details'), [(1,)])

    async def test_unavailable_agents_wait_without_spending_attempts(self):
        worker = self.worker()
        await self.ready(worker)
        staging = self.root/'staging'; staging.mkdir()
        self.store.update({'storage': {'user_evaluation_root':str(staging)},
                           'transcode': {'mode':'remote','agents':['agent:8787'],'retries':0}}, expected_revision=3)
        await worker.attempt()
        with patch.object(client.Remote, 'health', side_effect=ConnectionError()), \
             patch.object(client, 'target_bytes', return_value=1), patch.object(client, 'video_kbps', return_value=100):
            await worker.attempt()
        self.assertEqual(self.rows("SELECT state,last_reason_code FROM jobs WHERE kind='TRANSCODE_MEDIA'"), [('RETRY_WAIT','TRANSCODE_WAITING')])
        self.assertEqual(self.rows('SELECT attempts FROM transcode_media'), [(0,)])
