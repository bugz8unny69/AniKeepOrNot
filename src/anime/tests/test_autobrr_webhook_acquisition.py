"""Regression: an ordinary autobrr webhook reaches an owned qBittorrent torrent.

Real SQLite, real webhook normalization, real EVALUATE/RESERVE/PREPARE/SUBMIT
handlers, real magnet bootstrap and journal. Only the qBittorrent HTTP client
is replaced. No torrent_metadata.base_url is configured, which previously
ended every ordinary webhook in SERVICE_SETUP_INVALID.
"""
import hashlib
import json
import unittest

from src.anime.acquisition.legacy_magnet import make_bootstrap
from src.anime.acquisition.metadata import hash_magnet, parse_metainfo
from src.anime.acquisition.metainfo_source import make_reader
from src.anime.acquisition.submission import Observation
from src.anime.api.app import normalize_webhook
from src.anime.job_contracts import Policy, Registry
from src.anime.jobs.select_user_evaluation_job import registration as evaluate
from src.anime.jobs.prepare_acquisition_job import registration as prepare
from src.anime.jobs.reserve_episode_job import registration as reserve
from src.anime.jobs.submit_torrent_job import registration as submit
from src.anime.storage.sqlite import (accept_event_and_job, connect, enqueue_job,
                                        save_identity, transaction)
from src.anime.tests import test_acquisition_preparation as fixture
from src.anime.tests import test_torrent_submission as submission_fixture
from src.anime.worker import Worker

NYAA='http://nyaa.tracker.wf:7777/announce'
PASSKEY='https://private.example/0123456789abcdef0123456789abcdef/announce'


class FakeQbittorrent:
    """qBittorrent 4.x Web API behavior used by the existing adapter calls."""
    major=4
    metadata_strategy='guarded_export'

    def __init__(self,state,files):
        self.state,self.files=state,files

    def factory(self,options,credentials):
        client=FakeQbittorrent(self.state,self.files)
        client.options=options
        return client

    def login(self):pass

    def _observation(self,state,files):
        intent=self.state['intent']
        return Observation(intent.info_hash,state,intent.options.download_root,
                           intent.options.category,('ad-'+intent.marker,),False,files)

    def inspect(self,digest):
        if self.state.get('intent') is None:return None
        return self._observation('pausedDL',self.files)

    def add_legacy_magnet(self,magnet,intent,permit):
        permit.take(intent)
        self.state.setdefault('added',[]).append(magnet)
        self.state['intent']=intent

    def poll_legacy_metadata(self,intent,cancelled,*,attempts=24,interval=5):
        return self._observation('pausedDL',self.files)

    def stop_legacy_metadata(self,intent):
        return self._observation('pausedDL',self.files)

    def add_stopped(self,*args):
        raise AssertionError('an admitted magnet must never be submitted twice')


class AutobrrWebhookAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.AcquisitionPreparationTests.asyncSetUp
    rows=fixture.AcquisitionPreparationTests.rows

    def autobrr_event(self,body,episode=10):
        """Store the webhook exactly as the API does, then the verified facts
        IDENTIFY_ANIME/VERIFY_RELEASE would have produced (tested elsewhere)."""
        raw=normalize_webhook(body)
        db=connect(self.database)
        try:
            event,release=accept_event_and_job(db,'autobrr',raw,kind='VERIFY_RELEASE',payload={'anidb_id':123})
            identity=save_identity(db,event,{'schema_version':2,'anime':{
                'anidb_id':123,'tvdb_series_id':456,'title':'Test Anime'}})
            with transaction(db):
                db.execute("UPDATE jobs SET state='DONE',attempt_count=1 WHERE job_id=?",(release,))
                db.execute('INSERT OR IGNORE INTO identified_episodes VALUES(?,123)',(900+episode,))
                db.execute("INSERT INTO release_verifications(job_id,identity_result_id,anidb_id,release_sha256,"
                    "verification_scope,anidb_episode_number,anidb_episode_id,tvdb_series_id,tvdb_season,"
                    "tvdb_episode,tvdb_episode_id,verified_at_ms) "
                    "VALUES(?,?,123,?,'DECLARED_SINGLE_EPISODE',?,?,456,1,?,?,1)",
                    (release,identity,hashlib.sha256(raw).hexdigest(),episode,900+episode,episode,100+episode))
                enqueue_job(db,kind='SELECT_USER_EVALUATION',payload={'verification_job_id':release},
                    component='worker',event_id=event,created_by_job_id=release,available_at_ms=0)
            return event,raw
        finally:db.close()

    def test_webhook_keeps_only_a_sanitized_hash_bound_magnet(self):
        magnet=('magnet:?xt=urn:btih:'+self.digest.upper()+'&dn=Test+Anime+-+10'
                '&tr='+NYAA+'&tr='+PASSKEY+'&tr=http://t.example/announce?passkey=secret')
        stored=json.loads(normalize_webhook({'release_name':'Test Anime - 10',
            'torrentHash':self.digest.upper(),'magnet':magnet}))
        self.assertEqual(set(stored),{'release_name','torrent_infohash','magnet_uri'})
        self.assertIn('nyaa.tracker.wf',stored['magnet_uri'])
        self.assertNotIn('0123456789abcdef',stored['magnet_uri'])
        self.assertNotIn('passkey',stored['magnet_uri'])
        # A magnet for another torrent is dropped; the bare hash is used.
        other=json.loads(normalize_webhook({'release_name':'Test Anime - 10',
            'torrentHash':self.digest,'magnet':'magnet:?xt=urn:btih:'+'b'*40}))
        self.assertNotIn('magnet_uri',other)
        self.assertEqual(hash_magnet(self.digest),'magnet:?xt=urn:btih:'+self.digest)
        self.assertIsNone(hash_magnet(self.digest,'magnet:?xt=urn:btih:'+'b'*40))

    async def test_autobrr_webhook_without_metadata_source_reaches_owned_torrent(self):
        self.store.update({'qbittorrent':{'base_url':'https://qb.example'},
                           'storage':{'download_root':'/srv/downloads'}},expected_revision=1)
        files=parse_metainfo(self.raw,self.digest).files
        state={}
        qb=FakeQbittorrent(state,files)
        secrets=submission_fixture.Secrets()
        read=make_reader(secrets=secrets)
        worker=Worker(self.store,self.store.read(),Registry((
            evaluate(reservation_kind='RESERVE_EPISODE'),
            reserve(preparation_kind='PREPARE_ACQUISITION'),
            prepare(read,submission_kind='SUBMIT_TORRENT',
                    legacy_bootstrap=make_bootstrap(secrets,client_factory=qb.factory)),
            submit(read,secrets,client_factory=qb.factory))),policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(worker.close)
        # Real-shaped autobrr delivery: hash only, no magnet macro configured.
        event,_=self.autobrr_event({'release_name':'[SubsPlease] Test Anime - 10 (1080p)',
                                    'torrentHash':self.digest.upper()})
        for _ in range(6):await worker.attempt()

        self.assertEqual(state['added'],['magnet:?xt=urn:btih:'+self.digest])
        self.assertEqual(self.rows("SELECT kind,state FROM jobs WHERE event_id=? AND kind IN "
                                   "('PREPARE_ACQUISITION','SUBMIT_TORRENT') ORDER BY job_id",(event,)),
                         [('PREPARE_ACQUISITION','DONE'),('SUBMIT_TORRENT','DONE')])
        self.assertEqual(self.rows('SELECT state FROM torrent_submissions'),[('OWNED_STOPPED',)])
        marker=self.rows('SELECT submission_marker FROM acquisition_plans')[0][0]
        self.assertEqual(self.rows('SELECT marker,state FROM magnet_intents'),
                         [(marker,'OWNED_STOPPED')])
        self.assertEqual(self.rows("SELECT count(*) FROM job_history WHERE reason_code="
                                   "'SERVICE_SETUP_INVALID'"),[(0,)])
        self.assertEqual(self.rows("SELECT count(*) FROM jobs WHERE state NOT IN ('DONE')"),[(0,)])

    async def test_declared_magnet_with_trackers_is_what_qbittorrent_receives(self):
        self.store.update({'qbittorrent':{'base_url':'https://qb.example'},
                           'storage':{'download_root':'/srv/downloads'}},expected_revision=1)
        state={}
        qb=FakeQbittorrent(state,parse_metainfo(self.raw,self.digest).files)
        secrets=submission_fixture.Secrets()
        read=make_reader(secrets=secrets)
        worker=Worker(self.store,self.store.read(),Registry((
            evaluate(reservation_kind='RESERVE_EPISODE'),
            reserve(preparation_kind='PREPARE_ACQUISITION'),
            prepare(read,legacy_bootstrap=make_bootstrap(secrets,client_factory=qb.factory)))),
            policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(worker.close)
        self.autobrr_event({'release_name':'Test Anime - 10','torrentHash':self.digest,
                            'magnet':'magnet:?xt=urn:btih:'+self.digest+'&tr='+NYAA+'&tr='+PASSKEY})
        for _ in range(3):await worker.attempt()
        self.assertEqual(len(state['added']),1)
        self.assertIn('nyaa.tracker.wf',state['added'][0])
        self.assertNotIn('private.example',state['added'][0])
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='PREPARE_ACQUISITION'"),[('DONE',)])


if __name__=='__main__':
    unittest.main()
