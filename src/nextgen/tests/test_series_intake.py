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
                tvdb_series_id INTEGER,tvdb_season INTEGER,tvdb_episode INTEGER);
            CREATE TABLE job_operation_members(job_id INTEGER,operation_id INTEGER);
            CREATE TABLE job_operations(operation_id INTEGER,state TEXT);
            CREATE TABLE management_actions(operation_id INTEGER,action TEXT,job_id INTEGER);
            CREATE TABLE jobs(job_id INTEGER,state TEXT,kind TEXT,payload_json TEXT,last_reason_code TEXT,attempt_count INTEGER);
            CREATE TABLE anime_review_decisions(anidb_id INTEGER,job_id INTEGER,
                reason_text TEXT,cleanup_state TEXT);
            CREATE TABLE batch_staging_publications(plan_id INTEGER,file_index INTEGER,
                job_id INTEGER,state TEXT);
            CREATE TABLE batch_episode_reservations(plan_id INTEGER,file_index INTEGER,
                anidb_id INTEGER,tvdb_series_id INTEGER,tvdb_season INTEGER,tvdb_episode INTEGER);
            CREATE TABLE trial_plans(plan_id INTEGER,anidb_id INTEGER,tvdb_season INTEGER,
                anchor_episode INTEGER,backfill_count INTEGER,following_count INTEGER);
            CREATE TABLE sonarr_backfill_results(plan_id INTEGER,tvdb_episode INTEGER,status TEXT);
            INSERT INTO identified_anime VALUES(10,200,'Example Anime');
            INSERT INTO identity_results VALUES(10,
                '{"anime":{"english_title":"Example English","type":"TV","year":2026}}',1);
            INSERT INTO job_operations VALUES(11,'STAGED'),(12,'STAGED');
            INSERT INTO job_operation_members VALUES(101,11),(102,12);
            INSERT INTO episode_reservations VALUES(1,10,200,2,4),(2,10,200,2,5);
            INSERT INTO acquisition_plans VALUES(201,1),(202,2);
            INSERT INTO staging_publications VALUES(201,101,'PUBLISHED'),(202,102,'PUBLISHED');
            INSERT INTO trial_plans VALUES(300,10,2,5,6,3);
            ALTER TABLE trial_plans ADD COLUMN anchor_verification_id INTEGER;
            CREATE TABLE IF NOT EXISTS trial_evaluations(job_id INTEGER,verification_id INTEGER,plan_id INTEGER);
            CREATE TABLE IF NOT EXISTS job_history(job_id INTEGER,event_code TEXT);
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
        self.assertTrue(any(x['episode']==1 and x['can_retry'] for x in info['missing']))
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

    def test_missing_series_is_not_invented(self):
        self.assertIsNone(detail(self.db, 999))
