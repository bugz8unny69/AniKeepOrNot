"""Offline episode reset regressions. CI is intentionally not invoked for this work."""
import unittest

from src.nextgen.history import append_job_history
from src.nextgen.storage.episode_reset import (
    schedule, progress, find_episode, can_replay, EpisodeResetUnavailable,
)
from src.nextgen.storage.manual_release import submit
from src.nextgen.storage.sqlite import connect, transaction, enqueue_job_with_receipt
from src.nextgen.tests.test_api_v1 import ApiV1Tests
from src.nextgen.tests.test_management_actions import reserve_without_metadata


class EpisodeResetTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ApiV1Tests.asyncSetUp
    rows = ApiV1Tests.rows

    async def seeded_episode(self):
        await reserve_without_metadata(self)
        db=connect(self.database)
        try:
            row=db.execute(
                'SELECT r.anidb_id,r.tvdb_season,r.tvdb_episode,r.tvdb_episode_id,'
                'v.anidb_episode_number,v.anidb_episode_id '
                'FROM episode_reservations r JOIN release_verifications v '
                'USING(verification_id)'
            ).fetchone()
        finally:
            db.close()
        return row

    async def test_inspect_and_queue_reset_does_not_directly_release_owned_resources(self):
        aid,season,ep,tid,anidb_ep,anidb_eid=await self.seeded_episode()
        with connect(self.database) as db:
            tvdb=find_episode(db,tid)
            anidb=find_episode(db,anidb_eid)
            self.assertEqual(tvdb['anidb_episode'],anidb_ep)
            self.assertEqual(tvdb['plan_id'],anidb['plan_id'])
            self.assertEqual(len(tvdb['reservations']),1)

        receipt=schedule(self.database,aid,anidb_ep)
        self.assertFalse(receipt['reused'])
        self.assertEqual(schedule(self.database,aid,anidb_ep)['job_id'],receipt['job_id'])
        self.assertEqual(self.rows(
            'SELECT state FROM episode_reservations'),[('ACTIVE',)])
        self.assertEqual(self.rows(
            'SELECT count(*) FROM sonarr_backfill_results'),[(0,)])
        # Request only the existing cancellation + cleanup. The reservation
        # remains ACTIVE until the cleanup worker proves its operation drained.
        self.assertEqual(progress(self.database,tvdb['plan_id'],anidb_ep),'WAIT')
        self.assertEqual(self.rows(
            'SELECT state FROM episode_reservations'),[('ACTIVE',)])
        self.assertEqual(self.rows(
            "SELECT count(*) FROM jobs WHERE kind='CLEANUP_CANCELED_OPERATION'"),[(1,)])
        self.assertEqual(progress(self.database,tvdb['plan_id'],anidb_ep),'WAIT')
        self.assertEqual(self.rows(
            "SELECT count(*) FROM jobs WHERE kind='CLEANUP_CANCELED_OPERATION'"),[(1,)])

    async def test_size_failure_can_be_replayed_without_erasing_prior_event(self):
        aid,season,ep,tid,anidb_ep,anidb_eid=await self.seeded_episode()
        title=f'Test Anime S{season:02d}E{ep:02d} 1080p'
        manual=submit(self.database,aid,season,ep,release_name=title,
                      magnet_uri='magnet:?xt=urn:btih:'+'f'*40)
        db=connect(self.database)
        try:
            with transaction(db):
                job,_=enqueue_job_with_receipt(
                    db,kind='PREPARE_ACQUISITION',event_id=manual['event_id'],
                    payload={'evaluation_job_id':1},component='test')
                append_job_history(
                    db,job_id=job,event_code='FAILED',
                    reason_code='ACQUISITION_SIZE_LIMIT',
                    component='worker',occurred_at_ms=1000,
                    details={'attempt':0}
                )
            plan=db.execute(
                'SELECT plan_id FROM trial_plans WHERE anidb_id=?',(aid,)
            ).fetchone()[0]
            self.assertTrue(can_replay(db,plan,anidb_ep,aid,season,ep))
        finally:
            db.close()
        receipt=schedule(self.database,aid,anidb_ep,mode='replay')
        self.assertFalse(receipt['reused'])
        self.assertEqual(self.rows('SELECT count(*) FROM manual_release_submissions'),[(1,)])
        self.assertEqual(self.rows('SELECT state FROM episode_reservations'),[('ACTIVE',)])

    async def test_replay_requires_a_persisted_size_failure(self):
        aid,season,ep,tid,anidb_ep,anidb_eid=await self.seeded_episode()
        with self.assertRaises(EpisodeResetUnavailable):
            schedule(self.database,aid,anidb_ep,mode='replay')
