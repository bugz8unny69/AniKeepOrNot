"""Offline series grouping: no new Keep engine, just existing staged operations."""
import json
import sqlite3
import unittest

from src.nextgen.storage.series_intake import detail, list_items, keep_targets, IntakeUnavailable


class SeriesIntakeTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript("""
            CREATE TABLE identified_anime(anidb_id INTEGER,tvdb_series_id INTEGER,title TEXT);
            CREATE TABLE anime_title_metadata(anidb_id INTEGER,english_title TEXT,romaji_title TEXT);
            CREATE TABLE identity_results(anidb_id INTEGER,series_json TEXT,validated_at_ms INTEGER);
            CREATE TABLE staging_publications(plan_id INTEGER,job_id INTEGER,state TEXT);
            CREATE TABLE acquisition_plans(plan_id INTEGER,reservation_id INTEGER);
            CREATE TABLE episode_reservations(reservation_id INTEGER,anidb_id INTEGER,
                tvdb_series_id INTEGER,tvdb_season INTEGER,tvdb_episode INTEGER,
                owner_job_id INTEGER,state TEXT DEFAULT 'ACTIVE',
                anidb_episode_id INTEGER,tvdb_episode_id INTEGER);
            CREATE TABLE job_operation_members(job_id INTEGER,operation_id INTEGER);
            CREATE TABLE job_operations(operation_id INTEGER,state TEXT);
            CREATE TABLE management_actions(operation_id INTEGER,action TEXT,job_id INTEGER);
            CREATE TABLE jobs(job_id INTEGER,state TEXT,kind TEXT,payload_json TEXT,last_reason_code TEXT,attempt_count INTEGER,created_at_ms INTEGER);
            CREATE TABLE anime_review_decisions(anidb_id INTEGER,job_id INTEGER,
                reason_text TEXT,cleanup_state TEXT);
            CREATE TABLE batch_staging_publications(plan_id INTEGER,file_index INTEGER,
                job_id INTEGER,state TEXT);
            CREATE TABLE batch_episode_reservations(plan_id INTEGER,file_index INTEGER,
                anidb_id INTEGER,tvdb_series_id INTEGER,tvdb_season INTEGER,tvdb_episode INTEGER,
                reservation_id INTEGER,owner_job_id INTEGER,state TEXT DEFAULT 'ACTIVE',
                anidb_episode_id INTEGER,tvdb_episode_id INTEGER);
            CREATE TABLE trial_plans(plan_id INTEGER,anidb_id INTEGER,tvdb_season INTEGER,
                anchor_episode INTEGER,backfill_count INTEGER,following_count INTEGER);
            CREATE TABLE sonarr_backfill_results(plan_id INTEGER,anidb_episode_number INTEGER,status TEXT);
            CREATE TABLE sonarr_episode_selections(plan_id INTEGER,anidb_episode_number INTEGER,
                tvdb_season INTEGER,tvdb_episode INTEGER);
            -- AniDB E4/E5 are verified as TVDB S02E04/S02E05; E1-E3 have no placement yet.
            CREATE TABLE recognized_episodes(anidb_id INTEGER,anidb_episode_number INTEGER,
                tvdb_season INTEGER,tvdb_episode INTEGER);
            INSERT INTO recognized_episodes VALUES(10,4,2,4),(10,5,2,5);
            INSERT INTO identified_anime VALUES(10,200,'Example Anime');
            INSERT INTO identity_results VALUES(10,
                '{"anime":{"english_title":"Example English","type":"TV","year":2026}}',1);
            INSERT INTO job_operations VALUES(11,'STAGED'),(12,'STAGED');
            INSERT INTO job_operation_members VALUES(101,11),(102,12);
            INSERT INTO episode_reservations VALUES(1,10,200,2,4,101,'ACTIVE',10004,20004),
                (2,10,200,2,5,102,'ACTIVE',10005,20005);
            INSERT INTO acquisition_plans VALUES(201,1),(202,2);
            INSERT INTO staging_publications VALUES(201,101,'PUBLISHED'),(202,102,'PUBLISHED');
            INSERT INTO trial_plans VALUES(300,10,2,5,6,3);
            ALTER TABLE trial_plans ADD COLUMN anchor_verification_id INTEGER;
            CREATE TABLE IF NOT EXISTS trial_evaluations(job_id INTEGER,verification_id INTEGER,plan_id INTEGER,selected INTEGER);
            CREATE TABLE release_verifications(verification_id INTEGER,anidb_id INTEGER,
                tvdb_series_id INTEGER,tvdb_season INTEGER,tvdb_episode INTEGER,
                anidb_episode_id INTEGER,tvdb_episode_id INTEGER,
                anidb_episode_number INTEGER,tvdb_order TEXT);
            CREATE TABLE IF NOT EXISTS job_history(job_id INTEGER,event_code TEXT);
            CREATE TABLE IF NOT EXISTS manual_release_submissions(anidb_id INTEGER,tvdb_season INTEGER,tvdb_episode INTEGER);
            INSERT INTO sonarr_backfill_results VALUES(300,1,'NO_RESULTS');
        """)

    def tearDown(self):
        self.db.close()

    def test_two_releases_one_series_and_one_keep_selection(self):
        rows = list_items(self.db, 100, 30)
        self.assertEqual(len(rows['items']), 1)
        self.assertEqual(rows['items'][0]['staged_count'], 2)
        info = detail(self.db, 10)
        self.assertEqual(info['english_title'], 'Example English')
        self.assertEqual([(x['season'], x['episode']) for x in info['episodes']],
                         [(2, 4), (2, 5)])
        self.assertEqual(info['keep_operations'], [11, 12])
        self.assertEqual(keep_targets(self.db, 10, [11, 12]), [101, 102])
        with self.assertRaises(IntakeUnavailable):
            keep_targets(self.db, 10, [11, 11])
        with self.assertRaises(IntakeUnavailable):
            keep_targets(self.db, 10, [11, 13])

    def test_kept_episodes_are_not_resubmitted(self):
        self.db.execute("INSERT INTO jobs(job_id,state) VALUES(401,'DONE')")
        self.db.execute("INSERT INTO management_actions VALUES(11,'KEEP',401)")
        info = detail(self.db, 10)
        self.assertEqual(info['keep_operations'], [12])
        self.assertEqual(info['episodes'][0]['status'], 'Kept')
        self.assertTrue(any(x['anidb_episode']==1 and x['can_retry'] for x in info['missing']))
        with self.assertRaises(IntakeUnavailable):
            keep_targets(self.db, 10, [11])

    def test_rejected_series_remains_visible_but_blocks_keep_and_retry(self):
        self.db.execute("INSERT INTO jobs(job_id,state) VALUES(900,'DONE')")
        self.db.execute("INSERT INTO anime_review_decisions VALUES"
                        "(10,900,'Not interested: Not my cup of tea','COMPLETE')")
        info=detail(self.db,10)
        self.assertEqual(info['rejection']['reason'],
                         'Not interested: Not my cup of tea')
        self.assertEqual(info['rejection']['job_id'],900)
        self.assertEqual(info['keep_operations'],[])
        self.assertEqual(info['reject_operations'],[])
        self.assertTrue(all(not episode['can_retry'] for episode in info['missing']))
        self.assertEqual(list_items(self.db,100,30)['items'][0]['rejection']['job_id'],900)
        with self.assertRaises(IntakeUnavailable):
            keep_targets(self.db,10,[11])

    def test_language_switch_preserves_canonical_and_operations(self):
        self.db.execute("INSERT INTO anime_title_metadata VALUES(10,'English Name','Romaji Name')")
        english = detail(self.db, 10, language='english')
        japanese = detail(self.db, 10, language='japanese')
        self.assertEqual(english['title'], 'English Name')
        self.assertEqual(japanese['title'], 'Romaji Name')
        self.assertEqual(english['keep_operations'], japanese['keep_operations'])
        self.assertEqual(list_items(self.db, 100, 30, language='japanese')['items'][0]['title'], 'Romaji Name')

    def test_no_candidates_status_is_visible_on_intake(self):
        self.db.execute("INSERT INTO sonarr_backfill_results VALUES"
                        "(300,2,'WAITING_FOR_DUB')")
        info = detail(self.db, 10)
        episode = next(x for x in info['missing'] if x['anidb_episode'] == 2)
        self.assertEqual((episode['status'], episode['can_retry']),
                         ('WAITING_FOR_DUB', True))
        self.assertEqual(list_items(self.db, 100, 30)['items'][0]['waiting_for_dub'], 1)


    def test_failed_anchor_is_visible_with_owner_and_three_held_releases(self):
        # EP5 is the frozen anchor: automatic backfill intentionally omitted it.
        # Publication was never achieved; its active reservation must not hide it.
        self.db.executescript("""
            DELETE FROM staging_publications WHERE plan_id=202;
            DELETE FROM episode_reservations WHERE reservation_id=2;
            INSERT INTO job_operations VALUES(109,'ACTIVE'),(910,'ACTIVE'),(911,'ACTIVE'),(912,'ACTIVE');
            INSERT INTO job_operation_members VALUES(882,109),(1585,109),
                (2001,910),(2002,911),(2003,912);
            INSERT INTO jobs(job_id,state,kind,last_reason_code,attempt_count)
                VALUES(882,'DONE','RESERVE_EPISODE',NULL,1),
                      (1585,'NEEDS_REVIEW','PREPARE_ACQUISITION','TORRENT_METADATA_TIMEOUT',1);
            INSERT INTO episode_reservations VALUES(99,10,200,2,5,882,'ACTIVE',10005,20005);
            INSERT INTO release_verifications VALUES(51,10,200,2,5,10005,20005,5,'official'),
                (52,10,200,2,5,10005,20005,5,'official'),(53,10,200,2,5,10005,20005,5,'official');
            INSERT INTO trial_evaluations VALUES(901,51,300,1),(902,52,300,1),(903,53,300,1);
            INSERT INTO jobs(job_id,state,kind,payload_json,last_reason_code,attempt_count,created_at_ms)
                VALUES(2001,'ON_HOLD','RESERVE_EPISODE','{"evaluation_job_id":901}',
                    'EPISODE_RESERVED',1,100),
                      (2002,'ON_HOLD','RESERVE_EPISODE','{"evaluation_job_id":902}',
                    'EPISODE_RESERVED',1,200),
                      (2003,'ON_HOLD','RESERVE_EPISODE','{"evaluation_job_id":903}',
                    'EPISODE_RESERVED',1,300);
        """)
        info = detail(self.db, 10)
        anchor = next(x for x in info['missing'] if x['anidb_episode'] == 5)
        self.assertEqual(anchor['status'], 'NEEDS_REVIEW')
        self.assertEqual(anchor['reason_code'], 'TORRENT_METADATA_TIMEOUT')
        self.assertEqual(anchor['job_id'], 1585)
        self.assertEqual(anchor['reservation_id'], 99)
        self.assertEqual(anchor['waiting_job_ids'], [2001, 2002, 2003])
        self.assertTrue(anchor['can_retry_job'])
        self.assertTrue(anchor['can_try_next'])
        self.assertFalse(anchor['can_retry'])
        self.assertEqual(list_items(self.db,100,30)['items'][0]['missing_count'],
                         len(info['missing']))

    def test_window_follows_anidb_numbers_across_a_tvdb_boundary(self):
        # AniDB E1000 (TVDB S02E01) anchors the plan: Intake lists AniDB E994-E1002.
        self.db.executescript("""
            DELETE FROM trial_plans;
            DELETE FROM sonarr_backfill_results;
            INSERT INTO trial_plans VALUES(300,10,2,1000,6,3,NULL);
            INSERT INTO recognized_episodes VALUES(10,999,1,999),(10,1000,2,1);
            INSERT INTO sonarr_episode_selections VALUES(300,1001,2,2);
            INSERT INTO sonarr_backfill_results VALUES(300,995,'NO_RESULTS');
        """)
        missing = detail(self.db, 10)['missing']
        self.assertEqual([x['anidb_episode'] for x in missing], list(range(994, 1003)))
        labels = {x['anidb_episode']: (x['season'], x['episode']) for x in missing}
        self.assertEqual((labels[999], labels[1000], labels[1001], labels[994]),
                         ((1, 999), (2, 1), (2, 2), (None, None)))
        self.assertEqual(next(x for x in missing if x['anidb_episode'] == 995)['status'], 'NO_RESULTS')
        self.assertFalse(next(x for x in missing if x['anidb_episode'] == 1000)['can_retry'])

    def test_missing_series_is_not_invented(self):
        self.assertIsNone(detail(self.db, 999))
