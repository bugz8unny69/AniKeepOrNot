"""Observed evidence handoff uses real SQLite/worker completion, no daemon."""
import unittest
from unittest.mock import patch

from src.nextgen.acquisition.contracts import AcquisitionProposal
from src.nextgen.acquisition.metadata import ObservedInventory, parse_metainfo
from src.nextgen.acquisition.settings import DownloadOptions
from src.nextgen.acquisition.submission import manifest_from_json, ClientInvalid
from src.nextgen.job_contracts import Outcome
from src.nextgen.jobs.prepare_acquisition_job import registration
from src.nextgen.storage.sqlite import connect, transaction
from src.nextgen.storage.submissions import _plan, _intent
from src.nextgen.tests.test_acquisition_preparation import AcquisitionPreparationTests


class ObservedAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=AcquisitionPreparationTests.asyncSetUp
    rows=AcquisitionPreparationTests.rows
    source=AcquisitionPreparationTests.source
    worker=AcquisitionPreparationTests.worker
    prepare_queue=AcquisitionPreparationTests.prepare_queue

    async def test_atomic_handoff_preserves_marker_and_never_invents_info_sha(self):
        marker='e'*32
        observed=ObservedInventory(self.digest,parse_metainfo(self.raw,self.digest).files,0)
        async def read(*args):
            self.fail('original admission must precede metadata refetch')
        async def bootstrap(source,context):
            db=connect(self.database)
            try:
                with transaction(db):
                    plan=db.execute('SELECT plan_id FROM episode_reservations WHERE reservation_id=?',
                                    (source.reservation_id,)).fetchone()[0]
                    db.execute('INSERT INTO magnet_intents '
                        '(event_id,job_id,plan_id,anidb_episode_number,info_hash,marker,api_major,base_url,'
                        'save_path,category,timeout_seconds,inventory_json,state,created_at_ms,updated_at_ms) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (source.event_id,context.claim.job_id,plan,10,self.digest,marker,4,
                         'https://qb.example','/downloads/ad-'+marker,'anime',10,
                         observed.json(),'OWNED_STOPPED',1,1))
            finally:db.close()
            return Outcome(value=AcquisitionProposal(source,observed,
                context.configuration.desired_revision,preparation_job_id=context.claim.job_id))
        from src.nextgen.job_contracts import Registry, Policy
        from src.nextgen.jobs.select_user_evaluation_job import registration as evaluate
        from src.nextgen.jobs.reserve_episode_job import registration as reserve
        from src.nextgen.jobs.submit_torrent_job import registration as submit
        from src.nextgen.acquisition.submission import Observation
        from src.nextgen.tests.test_torrent_submission import Secrets
        from src.nextgen.worker import Worker
        test=self
        class Client:
            major=4
            def __init__(self,options,credentials):self.options=options
            def login(self):pass
            def inspect(self,digest):
                return Observation(digest,'pausedDL',self.options.download_root,
                                   self.options.category,('ad-'+marker,),False,observed.files)
            def add_stopped(self,*args):test.fail('promotion must never submit again')
        worker=Worker(self.store,self.startup,Registry((
            evaluate(reservation_kind='RESERVE_EPISODE'),
            reserve(preparation_kind='PREPARE_ACQUISITION'),
            registration(read,legacy_bootstrap=bootstrap,submission_kind='SUBMIT_TORRENT'),
            submit(read,Secrets(),client_factory=Client))),policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(worker.close)
        event,_=await self.prepare_queue(worker)
        # This fixture begins after provider verification; Sonarr dispatch itself
        # is separately tested. All acquisition and submission persistence is real.
        with patch('src.nextgen.storage.magnet_intake._verified_candidate'):
            await worker.attempt()
        self.assertEqual(self.rows('SELECT info_sha256,submission_marker FROM acquisition_plans'),[(None,marker)])
        self.assertEqual(self.rows('SELECT state FROM torrent_submissions'),[('OWNED_STOPPED',)])
        await worker.attempt()
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='SUBMIT_TORRENT'"),[('DONE',)])
        preparation=self.rows("SELECT job_id FROM jobs WHERE kind='PREPARE_ACQUISITION'")[0][0]
        db=connect(self.database)
        try:
            plan=_plan(db,preparation,event)
            intent=_intent(db,plan)
        finally:db.close()
        self.assertEqual(plan.manifest,observed)
        self.assertEqual(intent.options,DownloadOptions('https://qb.example','/downloads/ad-'+marker,'anime',10))

    async def test_observed_proposal_without_durable_admission_is_rejected(self):
        observed=ObservedInventory(self.digest,parse_metainfo(self.raw,self.digest).files,0)
        async def read(*args):self.fail('must not read')
        async def bootstrap(source,context):
            return Outcome(value=AcquisitionProposal(source,observed,context.configuration.desired_revision))
        worker=self.worker(registration(read,legacy_bootstrap=bootstrap))
        await self.prepare_queue(worker)
        with patch('src.nextgen.storage.magnet_intake._verified_candidate'):
            await worker.attempt()
        self.assertEqual(self.rows('SELECT count(*) FROM acquisition_plans'),[(0,)])
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='PREPARE_ACQUISITION'"),[('NEEDS_REVIEW',)])

    def test_observed_serialization_cannot_claim_a_metainfo_sha(self):
        observed=ObservedInventory(self.digest,parse_metainfo(self.raw,self.digest).files,0)
        self.assertIsNone(observed.info_sha256)
        self.assertEqual(manifest_from_json(self.digest,None,observed.json()),observed)
        with self.assertRaises(ClientInvalid):
            manifest_from_json(self.digest,'f'*64,observed.json())
