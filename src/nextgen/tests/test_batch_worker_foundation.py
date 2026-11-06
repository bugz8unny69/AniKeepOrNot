"""Offline contract tests for safe batch release and stopped-torrent preflight."""
import threading
import unittest

from src.nextgen.acquisition.metadata import BatchInventory, TorrentFile
from src.nextgen.acquisition.settings import SizeLimits
from src.nextgen.acquisition.submission import Observation
from src.nextgen.acquisition.transfers import TransferConflict, TransferSnapshot, PieceOverlap
from src.nextgen.batch.provider_worker import (
    BatchVerificationCompletion, make_batch_verifier,
)
from src.nextgen.batch.transfer import plan_stopped_batch_probe
from src.nextgen.batch.verification import verify_batch_files
from src.nextgen.batch.window import plan_batch_user_evaluation_window
from src.nextgen.configuration_schema import ConfigurationSnapshot
from src.nextgen.jobs.verify_release_job import ReleaseCompletion, registration
from src.nextgen.providers.http import Credentials, Options
from src.nextgen.providers.releases import make_verifier
from src.nextgen.release_contracts import ReleaseInput, ReleaseNotReady, VerifiedRelease
from src.nextgen.user_evaluation_policy import Window
from src.nextgen.tests.test_acquisition_metadata import metadata


class FakeProvider:
    def episodes(self,aid):
        return {1:1001,2:1002}
    def mapping_xml(self):
        return b'<anime-list><anime anidbid="123" tvdbid="456" defaulttvdbseason="1"/></anime-list>'
    def episode_page(self,tid,page,secret):
        return ((2001,1,1),(2002,1,2)),False


class ProviderCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_shiki_pack_verifies_without_sending_a_single_episode_user_evaluation(self):
        files=[
            {b'length':100,b'path':[b'Season 01',b'Shiki S01E01.mkv']},
            {b'length':100,b'path':[b'Season 01',b'Shiki S01E02.mkv']},
            {b'length':100,b'path':[b'OVA',b'Shiki OVA 01.mkv']},
        ]
        raw,digest=metadata(name=b'Shiki',files=files)
        source=ReleaseInput(1,1,123,456,
            ('{"release_name":"Shiki (S01 + OVA)","torrent_infohash":"%s"}' % digest).encode())
        configuration=ConfigurationSnapshot(1,{'storage':{'database_path':'/tmp/batch-test.sqlite'}})
        seen=[]
        async def read(expected,configuration,cancelled):
            seen.append(expected)
            return raw
        pool=lambda *args:FakeProvider()
        batch=make_batch_verifier(Options('registeredclient',1),lambda:Credentials('private'),
                                  read,client_factory=pool)
        combined=make_verifier(Options('registeredclient',1),lambda:Credentials('private'),
                               batch_verifier=batch,client_factory=pool)
        result=await combined(source,configuration,threading.Event())
        self.assertIs(type(result),BatchVerificationCompletion)
        self.assertEqual(len(result.verified.files),2)
        self.assertEqual([f.tvdb_episode for f in result.verified.files],[1,2])
        self.assertEqual(seen,[digest])
        spec=registration(combined,user_evaluation_kind='EVALUATE_TRIAL')
        self.assertEqual(spec.route(ReleaseCompletion(result,7)),())

    async def test_batch_source_hash_required_without_arbitrary_url(self):
        async def read(*args):
            self.fail('Unbound source must not trigger a metadata request')
        batch=make_batch_verifier(Options('registeredclient',1),
                                  lambda:Credentials('private'),read,client_factory=lambda *args:FakeProvider())
        source=ReleaseInput(1,1,123,456,b'{"release_name":"Shiki S01 + OVA"}')
        with self.assertRaises(ReleaseNotReady):
            await batch(source,ConfigurationSnapshot(1,{'storage':{
                'database_path':'/tmp/batch-test.sqlite'}}),threading.Event())


class BatchProbeTests(unittest.TestCase):
    def setUp(self):
        self.marker='c'*32
        self.inventory=BatchInventory('a'*40,'b'*64,(
            TorrentFile(0,'Shiki/Season 01/Shiki S01E01.mkv',262144),
            TorrentFile(1,'Shiki/Season 01/Shiki S01E02.mkv',262144),
            TorrentFile(2,'Shiki/OVA/Shiki OVA 01.mkv',262144)))
        self.verified=verify_batch_files(self.inventory,client=FakeProvider(),
            credentials=Credentials('private'),anidb_id=123,
            tvdb_series_id=456,season=1)
        self.plan=plan_batch_user_evaluation_window(self.inventory,self.verified,
            window=Window(6,1),anchor_episode=1)

    def snapshot(self,*,state='stoppedDL',pieces=((0,0),(1,1),(2,2)),tags=None):
        observation=Observation('a'*40,state,'/anime','trial',
                               tuple(tags if tags is not None else ('ad-'+self.marker,)),
                               False,self.inventory.files)
        return TransferSnapshot(observation,(0,0,0),(0.0,0.0,0.0),
                                262144,tuple(pieces))

    def plan_probe(self,snapshot):
        return plan_stopped_batch_probe(self.inventory,self.verified,self.plan,snapshot,
            ownership_marker=self.marker,download_root='/anime',category='trial',
            limits=SizeLimits(1,3))

    def test_probe_preflight_selects_exactly_one_video_and_excludes_ova(self):
        value=self.plan_probe(self.snapshot())
        self.assertEqual(value.selected_index,0)
        self.assertEqual(value.priorities,(1,0,0))
        self.assertEqual(value.expected_file_count,3)

    def test_mutated_state_ownership_and_piece_overlap_are_rejected(self):
        for snapshot in (self.snapshot(state='downloading'),
                         self.snapshot(tags=()),
                         self.snapshot(pieces=((0,1),(1,1),(2,2)))):
            with self.subTest(snapshot=snapshot),self.assertRaises((TransferConflict,PieceOverlap)):
                self.plan_probe(snapshot)
