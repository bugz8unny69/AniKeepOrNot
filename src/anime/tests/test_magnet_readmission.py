"""Same-hash webhook recovery after real oversized cancellation/cleanup."""
import sqlite3
import unittest

from src.anime.acquisition.legacy_magnet import make_bootstrap
from src.anime.acquisition.metadata import TorrentFile
from src.anime.acquisition.metainfo_source import make_reader
from src.anime.cancellation_service import CancellationService
from src.anime.job_contracts import Policy, Registry
from src.anime.jobs.select_user_evaluation_job import registration as evaluate
from src.anime.jobs.reserve_episode_job import registration as reserve
from src.anime.jobs.prepare_acquisition_job import registration as prepare
from src.anime.jobs.submit_torrent_job import registration as submit
from src.anime.jobs.cleanup_canceled_operation_job import registration as cleanup
from src.anime.storage.sqlite import connect, transaction, initialize
from src.anime.storage.migrations import SQL_DIR, SCRIPTS, LATEST_VERSION, _structure
from src.anime.tests import test_autobrr_webhook_acquisition as webhook_fixture
from src.anime.tests.test_autobrr_webhook_acquisition import FakeQbittorrent
from src.anime.tests.test_torrent_submission import Secrets
from src.anime.worker import Worker


# The reverse of 064_renamed_stored_names.sql: back to the v62 names.
UNDO_V63 = """BEGIN IMMEDIATE;
ALTER TABLE episode_discovery_results RENAME TO sonarr_backfill_results;
ALTER TABLE episode_release_candidates RENAME TO sonarr_episode_candidates;
ALTER TABLE episode_release_selections RENAME TO sonarr_episode_selections;
ALTER TABLE magnet_intents RENAME TO sonarr_magnet_intents;
ALTER TABLE user_evaluation_plans RENAME TO trial_plans;
ALTER TABLE user_evaluation_checks RENAME TO trial_evaluations;
ALTER TABLE batch_user_evaluation_plans RENAME TO batch_trial_plans;
ALTER TABLE batch_user_evaluation_files RENAME TO batch_trial_files;
ALTER TABLE acquisition_budgets RENAME COLUMN user_evaluation_plan_id TO trial_plan_id;
DROP INDEX idx_user_evaluation_checks_plan;
CREATE INDEX idx_trial_evaluations_plan ON trial_evaluations(plan_id);
DROP INDEX idx_batch_user_evaluation_by_episode;
CREATE INDEX idx_batch_trial_by_episode ON batch_trial_files(plan_id,tvdb_episode);
PRAGMA user_version=62;
COMMIT;"""

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
        self.assertEqual(self.rows('SELECT state FROM magnet_intents'),[('RELEASED',)])
        old_marker=self.rows('SELECT marker FROM magnet_intents')[0][0]
        # Reproduce a cleanup completed by the old worker: its journal was retained as REVIEW.
        db=connect(self.database)
        try:
            with transaction(db):db.execute("UPDATE magnet_intents SET state='REVIEW'")
            # Undo v63 (renamed tables) so the hand-built older shapes below use their own names.
            # Undo v67 (paths only) and v66 (resources), then v63; v64 and v65 changed only codes, none used below.
            db.executescript('DROP TABLE resource_operations; DROP TABLE resources;')
            db.execute('PRAGMA user_version=63')
            db.executescript(UNDO_V63)
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
            # v70 re-keyed Recognized names; put back their v69 (and v46) shape.
            v69=sqlite3.connect(':memory:')
            for name in SCRIPTS[:69]:v69.executescript((SQL_DIR/name).read_text())
            recognized=[sql+';' for (sql,) in v69.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name IN ('recognized_aliases','recognized_episodes') "
                "AND sql IS NOT NULL ORDER BY type DESC")]
            v69.close()
            db.executescript('PRAGMA foreign_keys=OFF;BEGIN IMMEDIATE;DROP TABLE recognized_aliases;'
                             'DROP TABLE recognized_episodes;DROP TABLE recognized_series;'
                             +'\n'.join(recognized)+'COMMIT;PRAGMA foreign_keys=ON;')
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
                'DROP TABLE service_status;\n'
                'DROP TABLE notifications;\n'
                'DROP TABLE manual_release_archive;\n'
                'DROP TABLE batch_candidate_stops;\n'
                'DROP TABLE batch_candidate_events;\n'
                'DROP TABLE batch_candidates;\n'
                'DROP TABLE batch_candidate_scopes;\n'
                'ALTER TABLE anime_review_decisions DROP COLUMN reason_text;\n'
                'PRAGMA user_version=46;\nCOMMIT;')
            for name in SCRIPTS[46:]:
                db.executescript((SQL_DIR/name).read_text())
            self.assertEqual(db.execute('SELECT * FROM magnet_intents').fetchall(),before)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        finally:db.close()
        self.store.update({'acquisition':{'max_episode_mb':2}},expected_revision=2)
        second,_=self.autobrr_event({'release_name':'[Group] Test Anime - 10','torrentHash':self.digest})
        for _ in range(4):await worker.attempt()
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE event_id=? AND kind='SUBMIT_TORRENT'",(second,)),[('DONE',)])
        self.assertEqual(self.rows('SELECT event_id,state FROM magnet_intents ORDER BY event_id'),
                         [(first,'RELEASED'),(second,'OWNED_STOPPED')])
        self.assertEqual(self.rows('SELECT marker FROM magnet_intents WHERE event_id=?',(first,)),[(old_marker,)])
        self.assertEqual(len(state['added']),2)
        self.assertEqual(len(set(r[0] for r in self.rows('SELECT marker FROM magnet_intents'))),2)
        db=connect(self.database)
        try:
            with self.assertRaises(sqlite3.IntegrityError),transaction(db):
                db.execute("UPDATE magnet_intents SET state='UNCERTAIN' WHERE event_id=?",(first,))
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
