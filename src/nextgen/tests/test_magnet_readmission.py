"""Same-hash webhook recovery after real oversized cancellation/cleanup."""
import sqlite3
import unittest

from src.nextgen.acquisition.legacy_magnet import make_bootstrap
from src.nextgen.acquisition.metadata import TorrentFile
from src.nextgen.acquisition.metainfo_source import make_reader
from src.nextgen.cancellation_service import CancellationService
from src.nextgen.job_contracts import Policy, Registry
from src.nextgen.jobs.evaluate_trial_job import registration as evaluate
from src.nextgen.jobs.reserve_episode_job import registration as reserve
from src.nextgen.jobs.prepare_acquisition_job import registration as prepare
from src.nextgen.jobs.submit_torrent_job import registration as submit
from src.nextgen.jobs.cleanup_canceled_operation_job import registration as cleanup
from src.nextgen.storage.sqlite import connect, transaction, initialize
from src.nextgen.storage.migrations import SQL_DIR, SCRIPTS, LATEST_VERSION, _structure
from src.nextgen.tests import test_autobrr_webhook_acquisition as webhook_fixture
from src.nextgen.tests.test_autobrr_webhook_acquisition import FakeQbittorrent
from src.nextgen.tests.test_torrent_submission import Secrets
from src.nextgen.worker import Worker


class MagnetReadmissionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=webhook_fixture.AutobrrWebhookAcquisitionTests.asyncSetUp
    rows=webhook_fixture.AutobrrWebhookAcquisitionTests.rows
    autobrr_event=webhook_fixture.AutobrrWebhookAcquisitionTests.autobrr_event

    async def test_oversized_cleanup_then_fresh_same_hash_webhook(self):
        self.store.update({'qbittorrent':{'base_url':'https://qb.example'},
                           'storage':{'download_root':str(self.root/'downloads')},
                           'acquisition':{'max_episode_mb':1,'max_plan_mb':2}},expected_revision=1)
        state={}
        qb=FakeQbittorrent(state,(TorrentFile(0,'Test Anime - 10.mkv',1000001),))
        def factory(options,credentials):
            client=qb.factory(options,credentials)
            def delete(info_hash,marker,save_path):
                state['removed']=state.get('removed',0)+1
                state['intent']=None
                return 'deleted'
            client.delete_owned=delete
            return client
        secrets=Secrets()
        read=make_reader(secrets=secrets)
        def service(configuration,secrets,**kwargs):
            return CancellationService(configuration,secrets,client_factory=factory,**kwargs)
        worker=Worker(self.store,self.store.read(),Registry((
            evaluate(reservation_kind='RESERVE_EPISODE'),reserve(preparation_kind='PREPARE_ACQUISITION'),
            prepare(read,submission_kind='SUBMIT_TORRENT',legacy_bootstrap=make_bootstrap(secrets,client_factory=factory)),
            submit(read,secrets,client_factory=factory),cleanup(secrets,service_factory=service))),
            policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(worker.close)
        first,_=self.autobrr_event({'release_name':'Test Anime - 10','torrentHash':self.digest})
        for _ in range(4):await worker.attempt()
        self.assertEqual(state['removed'],1)
        self.assertEqual(self.rows('SELECT state FROM episode_reservations'),[('RELEASED',)])
        self.assertEqual(self.rows('SELECT state FROM sonarr_magnet_intents'),[('RELEASED',)])
        old_marker=self.rows('SELECT marker FROM sonarr_magnet_intents')[0][0]
        # Reproduce a cleanup completed by the old worker: its journal was retained as REVIEW.
        db=connect(self.database)
        try:
            with transaction(db):db.execute("UPDATE sonarr_magnet_intents SET state='REVIEW'")
            # Undo v56 (AniDB-keyed trial tables) so the v46 shape below is exact.
            reference=sqlite3.connect(':memory:')
            for name in SCRIPTS[:55]:reference.executescript((SQL_DIR/name).read_text())
            undo=['PRAGMA foreign_keys=OFF;','BEGIN IMMEDIATE;']
            for table,columns in (
                    ('trial_plans','*'),('sonarr_backfill_results','*'),('sonarr_episode_candidates','*'),
                    ('sonarr_magnet_intents','*'),
                    ('sonarr_episode_selections','plan_id,anidb_episode_number,tvdb_episode_id,info_hash,selected_at_ms')):
                ddl=reference.execute("SELECT sql FROM sqlite_master WHERE name=?",(table,)).fetchone()[0]
                undo+=[ddl.replace(table,table+'_v55',1)+';',
                       f'INSERT INTO {table}_v55 SELECT {columns} FROM {table};',
                       f'DROP TABLE {table};',f'ALTER TABLE {table}_v55 RENAME TO {table};']
            undo+=["CREATE UNIQUE INDEX idx_magnet_active_hash ON sonarr_magnet_intents(info_hash) WHERE state<>'RELEASED';",
                   'PRAGMA user_version=55;','COMMIT;','PRAGMA foreign_keys=ON;']
            reference.close()
            db.executescript('\n'.join(undo))
            # Install the published v46 table shape with this nonempty journal,
            # then exercise the real upgrade before attempting readmission.
            before=db.execute('SELECT * FROM sonarr_magnet_intents').fetchall()
            ddl=(SQL_DIR/'045_sonarr_magnet_intents.sql').read_text()
            ddl=ddl[ddl.index('CREATE TABLE sonarr_magnet_intents ('):]
            ddl=ddl[:ddl.index('\n);')+3].replace('sonarr_magnet_intents (','magnet_v46 (',1)
            db.executescript('BEGIN IMMEDIATE;\n'+ddl+'\n'
                'INSERT INTO magnet_v46 SELECT * FROM sonarr_magnet_intents;\n'
                'DROP TABLE sonarr_magnet_intents;\n'
                'ALTER TABLE magnet_v46 RENAME TO sonarr_magnet_intents;\n'
                'DROP TABLE manual_identity_matches;\n'
                'DROP TABLE anime_title_metadata;\n'
                'DROP TABLE batch_metadata_admissions;\n'
                'DROP TABLE prowlarr_search_snapshots;\n'
                'DROP TABLE manual_release_submissions;\n'
                'DROP TABLE identity_candidate_evidence;\n'
                'DROP TABLE batch_candidate_events;\n'
                'DROP TABLE batch_candidates;\n'
                'DROP TABLE batch_candidate_scopes;\n'
                'ALTER TABLE anime_review_decisions DROP COLUMN reason_text;\n'
                'PRAGMA user_version=46;\nCOMMIT;')
            db.executescript((SQL_DIR/'047_released_magnet_intents.sql').read_text())
            db.executescript((SQL_DIR/'048_prowlarr_backfill_statuses.sql').read_text())
            db.executescript((SQL_DIR/'049_manual_identity_matches.sql').read_text())
            db.executescript((SQL_DIR/'050_series_rejection_reasons.sql').read_text())
            db.executescript((SQL_DIR/'051_batch_early_metadata.sql').read_text())
            db.executescript((SQL_DIR/'052_anime_title_metadata.sql').read_text())
            db.executescript((SQL_DIR/'053_waiting_for_dub_results.sql').read_text())
            db.executescript((SQL_DIR/'054_prowlarr_search_snapshots.sql').read_text())
            db.executescript((SQL_DIR/'055_manual_release_submissions.sql').read_text())
            db.executescript((SQL_DIR/'056_anidb_first_selection.sql').read_text())
            db.executescript((SQL_DIR/'057_anidb_first_staging.sql').read_text())
            db.executescript((SQL_DIR/'058_failed_job_state.sql').read_text())
            db.executescript((SQL_DIR/'059_identity_candidate_evidence.sql').read_text())
            db.executescript((SQL_DIR/'060_batch_candidate_operations.sql').read_text())
            self.assertEqual(db.execute('SELECT * FROM sonarr_magnet_intents').fetchall(),before)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        finally:db.close()
        self.store.update({'acquisition':{'max_episode_mb':2}},expected_revision=2)
        second,_=self.autobrr_event({'release_name':'[Group] Test Anime - 10','torrentHash':self.digest})
        for _ in range(4):await worker.attempt()
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE event_id=? AND kind='SUBMIT_TORRENT'",(second,)),[('DONE',)])
        self.assertEqual(self.rows('SELECT event_id,state FROM sonarr_magnet_intents ORDER BY event_id'),
                         [(first,'RELEASED'),(second,'OWNED_STOPPED')])
        self.assertEqual(self.rows('SELECT marker FROM sonarr_magnet_intents WHERE event_id=?',(first,)),[(old_marker,)])
        self.assertEqual(len(state['added']),2)
        self.assertEqual(len(set(r[0] for r in self.rows('SELECT marker FROM sonarr_magnet_intents'))),2)
        db=connect(self.database)
        try:
            with self.assertRaises(sqlite3.IntegrityError),transaction(db):
                db.execute("UPDATE sonarr_magnet_intents SET state='UNCERTAIN' WHERE event_id=?",(first,))
        finally:db.close()


class MagnetMigrationTests(unittest.TestCase):
    def test_v46_upgrade_preserves_history_and_matches_fresh(self):
        old=sqlite3.connect(':memory:');fresh=sqlite3.connect(':memory:')
        self.addCleanup(old.close);self.addCleanup(fresh.close)
        for name in SCRIPTS[:46]:old.executescript((SQL_DIR/name).read_text())
        old.execute("INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES('test',X'61',1)")
        old.commit()
        before=old.execute('SELECT * FROM incoming_events').fetchall()
        for name in SCRIPTS[46:]:
            old.executescript((SQL_DIR/name).read_text())
        initialize(fresh)
        self.assertEqual(_structure(old),_structure(fresh))
        self.assertEqual(old.execute('SELECT * FROM incoming_events').fetchall(),before)
        self.assertEqual(old.execute('PRAGMA user_version').fetchone()[0],LATEST_VERSION)
        self.assertEqual(old.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_migration_failure_rolls_back_original_journal_table(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        for name in SCRIPTS[:46]:db.executescript((SQL_DIR/name).read_text())
        before=_structure(db)
        sql=(SQL_DIR/'047_released_magnet_intents.sql').read_text()
        sql=sql.replace('PRAGMA user_version=47;','SELECT * FROM missing_table;\nPRAGMA user_version=47;')
        with self.assertRaises(sqlite3.Error):db.executescript(sql)
        db.rollback()
        self.assertEqual(_structure(db),before)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],46)
