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
                    payload={'evaluation_job_id':1},component='worker')
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


class ResetJobWorkerTests(unittest.IsolatedAsyncioTestCase):
    """The reset job, run by a real worker. A webhook magnet's intent carries the
    anchor release's event, which is also the reset job's event: the reset must
    never treat itself (or its cleanup controls) as an owner to cancel."""
    asyncSetUp = ApiV1Tests.asyncSetUp
    rows = ApiV1Tests.rows
    seeded_episode = EpisodeResetTests.seeded_episode

    async def reset_on_anchor_magnet(self):
        aid,season,ep,tid,anidb_ep,anidb_eid=await self.seeded_episode()
        job=schedule(self.database,aid,anidb_ep)['job_id']
        db=connect(self.database)
        try:
            plan,event=db.execute("SELECT json_extract(payload_json,'$.plan_id'),event_id FROM jobs "
                                  "WHERE job_id=?",(job,)).fetchone()
            verify=db.execute("SELECT job_id FROM jobs WHERE event_id=? AND kind='VERIFY_RELEASE'",(event,)).fetchone()[0]
            db.execute("INSERT INTO sonarr_magnet_intents(event_id,job_id,plan_id,anidb_episode_number,info_hash,marker,"
                       "api_major,base_url,save_path,category,timeout_seconds,state,created_at_ms,updated_at_ms) "
                       "VALUES(?,?,?,?,?,?,5,'http://qb','/dl','anime',30,'RELEASED',1,1)",
                       (event,verify,plan,anidb_ep,'e'*40,'f'*32))
            db.commit()
        finally:
            db.close()
        return job,plan,anidb_ep

    def own_operation(self,job):
        return self.rows("SELECT o.operation_id,o.state FROM job_operation_members m "
                         "JOIN job_operations o USING(operation_id) WHERE m.job_id=?",(job,))[0]

    async def test_a_reset_never_targets_its_own_operation(self):
        from src.nextgen.storage.episode_reset import _owners, _scope
        job,plan,episode=await self.reset_on_anchor_magnet()
        own,_=self.own_operation(job)
        db=connect(self.database)
        try:
            aid,_,season,number,*_=_scope(db,plan,episode)
            owners=[op for op,_ in _owners(db,plan,episode,aid,season,number)]
            self.assertTrue(owners)                                   # The anchor's own operations remain.
            self.assertNotIn(own,owners)                              # Excluded as a control job...
            self.assertNotIn(own,[op for op,_ in _owners(db,plan,episode,aid,season,number,job_id=job)])  # ...and by id.
        finally:
            db.close()

    async def test_the_reset_job_waits_for_cleanup_instead_of_cancelling_itself(self):
        import asyncio
        from src.nextgen.jobs.reset_episode_search_job import registration
        from src.nextgen.job_contracts import Registry
        from src.nextgen.worker import Policy, Worker
        job,plan,episode=await self.reset_on_anchor_magnet()
        worker=Worker(self.store,self.startup,Registry([registration()]),policy=Policy(lease_ms=3000))
        self.addAsyncCleanup(worker.close)
        await asyncio.wait_for(worker.attempt(),10)
        self.assertEqual(self.own_operation(job)[1],'ACTIVE')                      # Not cancelled by itself.
        events=[code for code, in self.rows('SELECT event_code FROM job_history WHERE job_id=? ORDER BY history_id',(job,))]
        self.assertNotIn('CANCELLATION_REQUESTED',events)
        self.assertEqual(self.rows('SELECT state,last_reason_code FROM jobs WHERE job_id=?',(job,)),
                         [('RETRY_WAIT','CANCELLATION_PENDING')])                    # Waits for the owners' cleanup.
        cleanups=self.rows("SELECT json_extract(payload_json,'$.target_job_id') FROM jobs "
                           "WHERE kind='CLEANUP_CANCELED_OPERATION'")
        self.assertTrue(cleanups)
        self.assertNotIn((job,),cleanups)
        # A second pass does not target the cleanup controls it queued either.
        db=connect(self.database)
        try:
            with transaction(db):
                db.execute('UPDATE jobs SET available_at_ms=0 WHERE job_id=?',(job,))
        finally:
            db.close()
        await asyncio.wait_for(worker.attempt(),10)
        self.assertEqual(self.own_operation(job)[1],'ACTIVE')
        self.assertEqual(len(self.rows("SELECT job_id FROM jobs WHERE kind='CLEANUP_CANCELED_OPERATION'")),len(cleanups))


class DashboardHandlerCatalogTests(unittest.TestCase):
    def test_every_job_kind_has_a_dashboard_label(self):
        # A kind missing here shows as "Unknown job handler" in the dashboard.
        import pathlib, re
        from src.nextgen.dashboard.presentation import HANDLERS
        source=pathlib.Path(__file__).resolve().parents[1]
        kinds=set(re.findall(r"^KIND\s*=\s*['\"]([A-Z_]+)['\"]", '\n'.join(
            p.read_text() for p in source.rglob('*.py') if 'tests' not in p.parts), re.M))
        self.assertIn('RESET_EPISODE_SEARCH',kinds)
        missing=[]
        for kind in sorted(kinds):
            try:HANDLERS.require(kind)
            except Exception:missing.append(kind)
        self.assertEqual(missing,[])


class ResetJobCompletionTests(unittest.IsolatedAsyncioTestCase):
    """Once the owners' cleanup is complete, the reset finishes and queues the new search."""
    asyncSetUp = ApiV1Tests.asyncSetUp
    rows = ApiV1Tests.rows
    seeded_episode = EpisodeResetTests.seeded_episode

    async def test_a_ready_reset_completes_records_its_event_and_searches_again(self):
        import asyncio
        from src.nextgen.jobs.reset_episode_search_job import registration
        from src.nextgen.job_contracts import Registry
        from src.nextgen.worker import Policy, Worker
        aid,season,ep,tid,anidb_ep,anidb_eid=await self.seeded_episode()
        job=schedule(self.database,aid,anidb_ep)['job_id']
        db=connect(self.database)
        try:
            with transaction(db):
                # The earlier attempts' cleanup has finished: nothing owns the episode any more.
                db.execute("UPDATE job_operations SET state='CANCELED',cleanup_state='COMPLETE' "
                           "WHERE operation_id NOT IN (SELECT operation_id FROM job_operation_members WHERE job_id=?)",(job,))
                db.execute("UPDATE episode_reservations SET state='RELEASED',ended_at_ms=1,ended_by_job_id=?",(job,))
        finally:
            db.close()
        worker=Worker(self.store,self.startup,Registry([registration()]),policy=Policy(lease_ms=3000))
        self.addAsyncCleanup(worker.close)
        self.assertTrue(await asyncio.wait_for(worker.attempt(),10))   # No crash in finish.
        events=[code for code, in self.rows('SELECT event_code FROM job_history WHERE job_id=? ORDER BY history_id',(job,))]
        self.assertIn('EPISODE_SEARCH_RESET',events)
        self.assertEqual(self.rows('SELECT state FROM jobs WHERE job_id=?',(job,)),[('DONE',)])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE kind='DISCOVER_SONARR_BACKFILL' "
                                   "AND created_by_job_id=?",(job,)),[(1,)])


class RegisteredCodesTests(unittest.TestCase):
    def test_every_literal_event_and_reason_written_by_the_code_is_registered(self):
        # An unregistered event made the reset's finish() raise, which stopped the worker.
        import ast, pathlib
        from src.nextgen.messages import EVENTS
        from src.nextgen.reasons import REGISTRY
        bad=[]
        for path in pathlib.Path(__file__).resolve().parents[1].rglob('*.py'):
            if 'tests' in path.parts: continue
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node,ast.Call): continue
                for kw in node.keywords:
                    value=kw.value.value if isinstance(kw.value,ast.Constant) else None
                    if type(value) is not str: continue
                    if kw.arg=='event_code' and value not in EVENTS: bad.append((path.name,node.lineno,value))
                    if kw.arg=='reason_code' and value not in REGISTRY: bad.append((path.name,node.lineno,value))
                if (getattr(node.func,'id',None)=='Outcome' and len(node.args)>=2
                        and isinstance(node.args[1],ast.Constant) and type(node.args[1].value) is str
                        and node.args[1].value not in REGISTRY):
                    bad.append((path.name,node.lineno,node.args[1].value))
        self.assertEqual(bad,[])
