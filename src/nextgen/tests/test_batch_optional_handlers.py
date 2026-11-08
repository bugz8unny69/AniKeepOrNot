"""Construction and routing tests for the default-enabled batch worker chain."""
import unittest

from src.nextgen.dashboard.presentation import HANDLERS
from src.nextgen.__main__ import parser
from src.nextgen.jobs.reserve_batch_job import (
    registration as reserve_batch, BatchReservationCompletion,
)
from src.nextgen.job_contracts import Registry
from src.nextgen.jobs.submit_batch_job import registration as submit_batch
from src.nextgen.jobs.transfer_batch_job import registration as transfer_batch
from src.nextgen.jobs.verify_batch_probe_job import registration as verify_probe
from src.nextgen.jobs.unlock_batch_job import registration as unlock_batch
from src.nextgen.jobs.verify_batch_episode_job import registration as verify_episode
from src.nextgen.jobs.stage_batch_episode_job import registration as stage_episode
from src.nextgen.jobs.keep_batch_job import registration as keep_batch
from src.nextgen.jobs.cleanup_batch_kept_media_job import registration as cleanup_kept
from src.nextgen.jobs.cleanup_batch_download_job import registration as cleanup_download


async def dummy_metainfo(*args):
    raise AssertionError('Construction may not fetch torrent metadata')


class BatchOptionalHandlersTests(unittest.TestCase):
    def test_opt_in_batch_chain_has_all_required_successors(self):
        registry=Registry((
            submit_batch(dummy_metainfo,object(),transfer_kind='TRANSFER_BATCH'),
            transfer_batch(dummy_metainfo,object(),
                           verification_kind='VERIFY_BATCH_PROBE'),
            verify_probe(dummy_metainfo,unlock_kind='UNLOCK_BATCH'),
            unlock_batch(dummy_metainfo,object()),
        ))
        self.assertIsNotNone(registry)
        for code in ('SUBMIT_BATCH','TRANSFER_BATCH','VERIFY_BATCH_PROBE',
                     'UNLOCK_BATCH'):
            HANDLERS.require(code)

    def test_full_batch_successor_contract_and_management_handlers(self):
        stages=(
            submit_batch(dummy_metainfo,object(),transfer_kind='TRANSFER_BATCH'),
            transfer_batch(dummy_metainfo,object(),
                           verification_kind='VERIFY_BATCH_PROBE'),
            verify_probe(dummy_metainfo,unlock_kind='UNLOCK_BATCH'),
            unlock_batch(dummy_metainfo,object(),next_kind='VERIFY_BATCH_EPISODE'),
            verify_episode(dummy_metainfo,staging_kind='STAGE_BATCH_EPISODE'),
            stage_episode(),
            keep_batch(object()),
            cleanup_kept(object()),cleanup_download(object()),
        )
        self.assertIsNotNone(Registry(stages))
        unlock=stages[3]
        jobs=unlock.route({
            'unlock_job_id':103,'file_indices':(0,2,4),
        })
        self.assertEqual(len(jobs),3)
        self.assertEqual([item.kind for item in jobs],
                         ['VERIFY_BATCH_EPISODE']*3)
        self.assertEqual([item.payload for item in jobs],[
            '{"file_index": 0, "unlock_job_id": 103}',
            '{"file_index": 2, "unlock_job_id": 103}',
            '{"file_index": 4, "unlock_job_id": 103}',
        ])
        self.assertEqual(stages[4].route({
            'verification_job_id':201,'file_index':2,
        })[0].kind,'STAGE_BATCH_EPISODE')
        self.assertIsNone(stages[5].route)
        for index in (6,7,8):
            self.assertTrue(callable(stages[index].execute))
            self.assertTrue(callable(stages[index].persist))

    def test_reservation_routes_its_own_job_id_not_user_evaluation_check_id(self):
        spec=reserve_batch(submission_kind='SUBMIT_BATCH')
        receipt=BatchReservationCompletion(source=None,reservation_job_id=789)
        routed=spec.route(receipt)
        self.assertEqual(len(routed),1)
        self.assertEqual(routed[0].kind,'SUBMIT_BATCH')
        self.assertEqual(routed[0].payload,'{"reservation_job_id": 789}')

    def test_worker_has_no_temporary_batch_switch(self):
        arguments=parser().parse_args(['worker','--config-dir','/tmp/ad-test'])
        self.assertFalse(hasattr(arguments,'no_batch_acquisition'))
        with self.assertRaises(SystemExit):
            parser().parse_args([
                'worker','--config-dir','/tmp/ad-test','--no-batch-acquisition'])

    def test_untrusted_sync_metadata_reader_is_rejected(self):
        for build in (
                lambda:submit_batch(lambda *_:None,object()),
                lambda:transfer_batch(lambda *_:None,object()),
                lambda:verify_probe(lambda *_:None),
                lambda:unlock_batch(lambda *_:None,object())):
            with self.subTest(build=build),self.assertRaises(ValueError):
                build()


if __name__=='__main__':
    unittest.main()
