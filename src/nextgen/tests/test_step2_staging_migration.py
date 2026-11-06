"""Offline v56 -> v57: AniDB-first staging without TVDB placement, TVDB required at Keep."""
import datetime
import json
import sqlite3
import unittest
import xml.etree.ElementTree as ET

from src.nextgen.history import integer
from src.nextgen.keep_selection import KeepSelection, KeepSelectionInvalid, load_selection
from src.nextgen.release_contracts import ReleaseInput, VerifiedRelease
from src.nextgen.staging import specification, nfo
from src.nextgen.storage.migrations import SQL_DIR, SCRIPTS, _structure, LATEST_VERSION, _apply_step
from src.nextgen.storage.sqlite import initialize
from src.nextgen.storage.user_evaluations import persist_user_evaluation
from src.nextgen.storage.episode_reservations import persist_reservation
from src.nextgen.storage.releases import persist_release
from src.nextgen.user_evaluation_policy import UserEvaluationSource, UserEvaluationProposal, Window
from src.nextgen.episode_reservations import ReservationInput
from types import SimpleNamespace

SCRIPT = (SQL_DIR / '057_anidb_first_staging.sql').read_text()


class Step2StagingMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        for name in SCRIPTS[:56]:
            self.db.executescript((SQL_DIR / name).read_text())
        h64 = 'a' * 64
        self.db.executescript(f"""
            INSERT INTO incoming_events(event_id,source,raw_body,received_at_ms) VALUES(1,'manual-release',X'61',1);
            INSERT INTO identified_anime VALUES(50,100,'Long Show');
            INSERT INTO identity_results VALUES(1,1,50,2,'{{"anime":{{"anidb_id":50,"tvdb_series_id":100}}}}',1);
            UPDATE incoming_events SET state='IDENTIFIED';
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(1,1,'VERIFY_RELEASE','{{}}','DONE',0,0,0);
            INSERT INTO identified_episodes VALUES(5001,50);
            INSERT INTO release_verifications(verification_id,job_id,identity_result_id,anidb_id,
              release_sha256,verification_scope,anidb_episode_number,anidb_episode_id,tvdb_series_id,
              tvdb_season,tvdb_episode,tvdb_episode_id,verified_at_ms)
              VALUES(1,1,1,50,'{h64}','DECLARED_SINGLE_EPISODE',1,5001,100,1,1,9001,1);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(2,1,'EVALUATE_TRIAL','{{}}','DONE',0,0,0);
            INSERT INTO trial_plans(plan_id,anidb_id,tvdb_series_id,tvdb_season,
              anchor_verification_id,anchor_episode,backfill_count,following_count,
              configuration_revision,created_at_ms) VALUES(1,50,100,1,1,1,6,3,1,0);
            INSERT INTO trial_evaluations(evaluation_id,job_id,verification_id,plan_id,selected,reason_code,evaluated_at_ms)
              VALUES(1,2,1,1,1,'TRIAL_SELECTED',1);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(3,1,'RESERVE_EPISODE','{{}}','DONE',0,0,0);
            INSERT INTO episode_reservations(reservation_id,owner_job_id,evaluation_job_id,verification_id,
              plan_id,anidb_id,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id,
              reserved_at_ms) VALUES(1,3,2,1,1,50,5001,100,1,1,9001,1);
            INSERT INTO sonarr_episode_selections(plan_id,anidb_episode_number,tvdb_season,tvdb_episode,
              tvdb_episode_id,info_hash,selected_at_ms) VALUES(1,1,1,1,9001,'{'f'*40}',1);
        """)
        self.db.commit()

    def migrate(self):
        self.db.executescript(SCRIPT)
        self.db.executescript((SQL_DIR / '058_failed_job_state.sql').read_text())
        self.db.executescript((SQL_DIR / '059_identity_candidate_evidence.sql').read_text())
        self.db.executescript((SQL_DIR / '060_batch_candidate_operations.sql').read_text())
        self.db.executescript((SQL_DIR / '061_batch_candidate_stops.sql').read_text())
        self.db.executescript((SQL_DIR / '062_service_status.sql').read_text())
        self.db.executescript((SQL_DIR / '063_renamed_stored_names.sql').read_text())
        self.db.executescript((SQL_DIR / '064_renamed_codes.sql').read_text())
        self.db.executescript((SQL_DIR / '065_retired_service_codes.sql').read_text())
        return self.db

    def test_fresh_schema_matches_upgrade_to_v57(self):
        fresh = sqlite3.connect(':memory:')
        self.addCleanup(fresh.close)
        initialize(fresh)
        upgraded = self.migrate()
        self.assertEqual(_structure(fresh), _structure(upgraded))
        self.assertEqual(upgraded.execute('PRAGMA user_version').fetchone()[0], LATEST_VERSION)

    def test_v57_permits_nullable_tvdb_fields(self):
        upgraded = self.migrate()
        # Insert a release verification without TVDB placement
        b64 = 'b' * 64
        e40 = 'e' * 40
        upgraded.executescript(f"""
            INSERT INTO identified_episodes VALUES(5002, 50);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(4,1,'VERIFY_RELEASE','{{}}','DONE',0,0,0);
            INSERT INTO release_verifications(verification_id,job_id,identity_result_id,anidb_id,
              release_sha256,verification_scope,anidb_episode_number,anidb_episode_id,tvdb_series_id,
              tvdb_season,tvdb_episode,tvdb_episode_id,verified_at_ms)
              VALUES(2,4,1,50,'{b64}','DECLARED_SINGLE_EPISODE',2,5002,100,NULL,NULL,NULL,2);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(5,1,'RESERVE_EPISODE','{{}}','DONE',0,0,0);
            INSERT INTO episode_reservations(reservation_id,owner_job_id,evaluation_job_id,verification_id,
              plan_id,anidb_id,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id,
              reserved_at_ms) VALUES(2,5,2,2,1,50,5002,100,NULL,NULL,NULL,2);
            INSERT INTO episode_release_selections(plan_id,anidb_episode_number,tvdb_season,tvdb_episode,
              tvdb_episode_id,info_hash,selected_at_ms) VALUES(1,2,NULL,NULL,NULL,'{e40}',2);
        """)
        upgraded.commit()
        row = upgraded.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM release_verifications WHERE verification_id=2").fetchone()
        self.assertEqual(row, (None, None, None))
        res_row = upgraded.execute("SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM episode_reservations WHERE reservation_id=2").fetchone()
        self.assertEqual(res_row, (None, None, None))

    def test_staging_specification_without_tvdb_placement(self):
        intent = SimpleNamespace(options=SimpleNamespace(download_root='/source'))
        evidence = {'path': 'video.mkv', 'sha256': 'c' * 64, 'file': [1, 2, 500]}
        # Identity: anime=50, episode=5002, series=100, season=None, number=2, tvdb_episode=None, title='Long Show'
        identity = (50, 5002, 100, None, 2, None, 'Long Show')
        plan = specification(intent, evidence, identity, '/staging', 1)
        self.assertIn('Season 01', plan['entries'][0]['path'])
        self.assertIn('Long Show - S01E02.mkv', plan['entries'][0]['path'])
        episode_xml = ET.fromstring(plan['entries'][1]['text'])
        # Anidb uniqueid present and marked default='true' when tvdb is missing
        uniqueids = [(e.attrib.get('type'), e.attrib.get('default'), e.text) for e in episode_xml.findall('uniqueid')]
        self.assertEqual(uniqueids, [('anidb', 'true', '5002')])

    def test_keep_halts_in_needs_review_when_tvdb_placement_is_missing(self):
        upgraded = self.migrate()
        # Create staged state for reservation with null TVDB placement
        d64 = 'd' * 64
        a40 = 'a' * 40
        b64 = 'b' * 64
        c32 = 'c' * 32
        f64 = 'f' * 64
        upgraded.executescript(f"""
            INSERT INTO identified_episodes VALUES(5003, 50);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(10,1,'VERIFY_RELEASE','{{}}','DONE',0,0,0);
            INSERT INTO release_verifications(verification_id,job_id,identity_result_id,anidb_id,
              release_sha256,verification_scope,anidb_episode_number,anidb_episode_id,tvdb_series_id,
              tvdb_season,tvdb_episode,tvdb_episode_id,verified_at_ms)
              VALUES(3,10,1,50,'{d64}','DECLARED_SINGLE_EPISODE',3,5003,100,NULL,NULL,NULL,3);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(11,1,'RESERVE_EPISODE','{{}}','DONE',0,0,0);
            INSERT INTO episode_reservations(reservation_id,owner_job_id,evaluation_job_id,verification_id,
              plan_id,anidb_id,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id,
              reserved_at_ms) VALUES(3,11,2,3,1,50,5003,100,NULL,NULL,NULL,3);
            INSERT INTO acquisition_plans(plan_id,job_id,reservation_id,info_hash,info_sha256,manifest_json,
              submission_marker,state,configuration_revision,prepared_at_ms)
              VALUES(3,11,3,'{a40}','{b64}','{{}}','{c32}','PREPARED',1,1);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(12,1,'DOWNLOAD_TORRENT','{{}}','DONE',0,0,0);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(13,1,'VERIFY_MEDIA','{{}}','DONE',0,0,0);
            INSERT INTO media_verifications(plan_id,job_id,download_job_id,submission_fingerprint,transfer_fingerprint,evidence_json,verified_at_ms)
              VALUES(3,13,12,'{f64}','{f64}','{{"path":"s/S01E03.mkv"}}',1);
            INSERT INTO job_operations(operation_id,state,created_at_ms,updated_at_ms) VALUES(20,'STAGED',1,1);
            INSERT INTO jobs(job_id,event_id,kind,payload_json,state,available_at_ms,created_at_ms,updated_at_ms)
              VALUES(14,1,'STAGE_TRIAL_MEDIA','{{}}','DONE',0,0,0);
            INSERT INTO job_operation_members(job_id,operation_id) VALUES(14,20);
            INSERT INTO staging_publications(plan_id,job_id,verification_job_id,manifest_json,receipts_json,state,created_at_ms,published_at_ms)
              VALUES(3,14,13,'{{"entries":[{{"role":"media","path":"s/S01E03.mkv"}}],"root":"/staging"}}',
                     '[{{"role":"media","path":"s/S01E03.mkv"}}]',
                     'PUBLISHED',1,1);
        """)
        upgraded.commit()
        # Attempting to load selection for Keep raises KeepSelectionInvalid because TVDB placement is None
        with self.assertRaises(KeepSelectionInvalid):
            load_selection(upgraded, 14)

        # But if late TVDB placement is discovered and recorded in recognized_episodes:
        upgraded.executescript("""
            INSERT INTO recognized_seasons(tvdb_series_id,tvdb_season,anidb_id,display_title,evidence_verification_id,recognized_at_ms)
              VALUES(100,1,50,'Long Show',1,1);
            INSERT INTO recognized_episodes(tvdb_series_id,tvdb_season,anidb_id,anidb_episode_number,
              anidb_episode_id,tvdb_episode,tvdb_episode_id,evidence_verification_id,recognized_at_ms)
              VALUES(100,1,50,3,5003,3,9003,1,1);
        """)
        upgraded.commit()
        selection = load_selection(upgraded, 14)
        self.assertEqual(selection.tvdb_season, 1)
        self.assertEqual(selection.tvdb_episode, 3)
        self.assertEqual(selection.tvdb_episode_id, 9003)


if __name__ == '__main__':
    unittest.main()
