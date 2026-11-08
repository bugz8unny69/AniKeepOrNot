"""Small SQLite tests for the immutable backfill selection receipt."""
import sqlite3
import unittest

from src.nextgen.acquisition.prowlarr_search import DiscoveryPolicy, Release, SearchResult
from src.nextgen.episode_identity import VerifiedEpisode
from src.nextgen.storage.release_selection import SelectionConflict, select

HASH = 'a' * 40
OTHER = 'b' * 40
EP = VerifiedEpisode(100, 200, 1, 10)


def release(digest=HASH, *, usable=True, seeders=10, episode_id=200):
    return Release('Example S01E10 1080p', 'Nyaa', 1080, seeders, 123456789,
                   episode_id, digest, 'magnet:?xt=urn:btih:' + digest, usable)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE trial_plans (
              plan_id INTEGER PRIMARY KEY, anidb_id INTEGER,
              tvdb_series_id INTEGER, tvdb_season INTEGER, tvdb_order TEXT,
              anchor_verification_id INTEGER,
              anchor_episode INTEGER, backfill_count INTEGER, following_count INTEGER);
            CREATE TABLE trial_evaluations (
              evaluation_id INTEGER PRIMARY KEY, job_id INTEGER,
              verification_id INTEGER, plan_id INTEGER);
            CREATE TABLE job_history (
              history_id INTEGER PRIMARY KEY, job_id INTEGER, event_code TEXT);
            CREATE TABLE sonarr_backfill_results (
              plan_id INTEGER, anidb_episode_number INTEGER, PRIMARY KEY(plan_id, anidb_episode_number));
            CREATE TABLE recognized_episodes (
              anidb_id INTEGER, tvdb_series_id INTEGER, tvdb_season INTEGER,
              tvdb_episode INTEGER, tvdb_episode_id INTEGER, anidb_episode_number INTEGER);
            CREATE TABLE episode_reservations (
              state TEXT, tvdb_series_id INTEGER, tvdb_season INTEGER, tvdb_episode INTEGER);
            CREATE TABLE batch_episode_reservations (
              state TEXT, tvdb_series_id INTEGER, tvdb_season INTEGER, tvdb_episode INTEGER);
            CREATE TABLE sonarr_episode_selections (
              plan_id INTEGER, anidb_episode_number INTEGER, tvdb_season INTEGER,
              tvdb_episode INTEGER, tvdb_episode_id INTEGER,
              info_hash TEXT, selected_at_ms INTEGER,
              PRIMARY KEY(plan_id,anidb_episode_number), UNIQUE(plan_id,info_hash));
            INSERT INTO trial_plans VALUES (1,50,100,1,'official',10,10,6,1);
            INSERT INTO trial_evaluations VALUES (1,1,10,1);
            INSERT INTO job_history VALUES (1,1,'TRIAL_AUDIO_SEARCH');
            INSERT INTO recognized_episodes VALUES (50,100,1,10,200,10);
        """)
        self.db.commit()

    def choose(self, selected=None, *, result=None, identity=EP, episode=10):
        chosen = selected or release()
        results = result or SearchResult(100, 200, (chosen,))
        return select(self.db,plan_id=1,episode=episode,identity=identity,result=results,
                      release=chosen,policy=DiscoveryPolicy())

    def test_selection_is_idempotent(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.assertTrue(self.choose())
            self.assertFalse(self.choose())
        self.assertEqual(self.db.execute(
            'SELECT anidb_episode_number,tvdb_season,tvdb_episode,tvdb_episode_id,info_hash '
            'FROM sonarr_episode_selections').fetchall(), [(10,1,10,200,HASH)])

    def test_selection_crosses_tvdb_seasons_by_anidb_number(self):
        # AniDB E8 at TVDB S02E02: in the AniDB window (anchor 10, backfill 6 -> 4-9).
        identity = VerifiedEpisode(100, 300, 2, 2)
        result = SearchResult(100, 300, (release(episode_id=300),))
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.assertTrue(select(self.db, plan_id=1, episode=8, identity=identity, result=result,
                                   release=result.candidates[0], policy=DiscoveryPolicy()))
        self.assertEqual(self.db.execute(
            'SELECT anidb_episode_number,tvdb_season,tvdb_episode FROM sonarr_episode_selections'
        ).fetchall(), [(8,2,2)])
        # AniDB E3 is outside the window even though TVDB S02E03 "looks" early.
        with self.db, self.assertRaises(SelectionConflict):
            self.db.execute('BEGIN IMMEDIATE')
            select(self.db, plan_id=1, episode=3, identity=VerifiedEpisode(100, 301, 2, 3),
                   result=SearchResult(100, 301, (release(OTHER, episode_id=301),)),
                   release=release(OTHER, episode_id=301), policy=DiscoveryPolicy())

    def test_conflict_does_not_replace_prior_selection(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.choose()
        with self.db, self.assertRaises(SelectionConflict):
            self.db.execute('BEGIN IMMEDIATE')
            self.choose(release(OTHER))
        self.assertEqual(self.db.execute(
            'SELECT info_hash FROM sonarr_episode_selections').fetchone()[0],HASH)

    def test_unrecognized_or_reserved_is_refused(self):
        with self.db, self.assertRaises(SelectionConflict):
            self.db.execute('BEGIN IMMEDIATE')
            self.choose(identity=VerifiedEpisode(100,201,1,10))
        self.db.execute("INSERT INTO episode_reservations VALUES ('ACTIVE',100,1,10)")
        with self.db, self.assertRaises(SelectionConflict):
            self.choose()
        self.db.execute("DELETE FROM episode_reservations")
        self.db.execute("INSERT INTO batch_episode_reservations VALUES ('ACTIVE',100,1,10)")
        with self.db, self.assertRaises(SelectionConflict):
            self.choose()

    def test_unbound_wrong_scope_and_low_seeders_rejected(self):
        for item in (release(usable=False), release(seeders=0), release(OTHER),
                     release(episode_id=201)):
            with self.subTest(item=item), self.db, self.assertRaises(SelectionConflict):
                self.db.execute('BEGIN IMMEDIATE')
                self.choose(selected=item, result=SearchResult(100,200,(release(),)))

    def test_requires_transaction(self):
        with self.assertRaises(SelectionConflict):
            self.choose()

    def test_migrated_frozen_window_accepted_during_selection(self):
        # PR #250 scenario: historical stored results 1..6 differ from recomputed window.
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sonarr_backfill_results (
                plan_id INTEGER, anidb_episode_number INTEGER);
            INSERT INTO sonarr_backfill_results VALUES (1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6);
            UPDATE trial_plans SET anchor_episode=1151, backfill_count=6, following_count=1 WHERE plan_id=1;
            INSERT INTO recognized_episodes VALUES (50,100,1,2,202,2);
        """)
        identity = VerifiedEpisode(100, 202, 1, 2)
        result = SearchResult(100, 202, (release(episode_id=202),))
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.assertTrue(select(self.db, plan_id=1, episode=2, identity=identity, result=result,
                                   release=result.candidates[0], policy=DiscoveryPolicy()))
        self.assertEqual(self.db.execute(
            'SELECT anidb_episode_number,tvdb_season,tvdb_episode FROM sonarr_episode_selections WHERE plan_id=1 AND anidb_episode_number=2'
        ).fetchone(), (2, 1, 2))

    def test_frozen_historical_window_rejects_recalculated_episode(self):
        # Plan 2 has anchor 1157, backfill 6, following 0 (recalculated window would be 1151..1156)
        # But frozen historical results in sonarr_backfill_results are 1..6
        self.db.execute("INSERT INTO trial_plans VALUES (2,50,100,1,'official',20,1157,6,0)")
        for ep in (1, 2, 3, 4, 5, 6):
            self.db.execute("INSERT INTO sonarr_backfill_results VALUES (2,?)", (ep,))
            self.db.execute("INSERT INTO recognized_episodes VALUES (50,100,1,?,200+?,?)", (ep, ep, ep))
        # Recalculated episode 1151 is also recognized
        self.db.execute("INSERT INTO recognized_episodes VALUES (50,100,1,1151,1351,1151)")
        self.db.commit()

        # Episode 1 is within frozen window: allowed
        ident1 = VerifiedEpisode(100, 201, 1, 1)
        res1 = SearchResult(100, 201, (release(episode_id=201),))
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.assertTrue(select(self.db, plan_id=2, episode=1, identity=ident1, result=res1,
                                   release=res1.candidates[0], policy=DiscoveryPolicy()))

        # Episode 1151 is outside frozen window (even though in recalculated Window(6,0).anidb_episodes(1157))
        # It must be strictly rejected with SelectionConflict
        ident1151 = VerifiedEpisode(100, 1351, 1, 1151)
        res1151 = SearchResult(100, 1351, (release(OTHER, episode_id=1351),))
        with self.db, self.assertRaises(SelectionConflict):
            self.db.execute('BEGIN IMMEDIATE')
            select(self.db, plan_id=2, episode=1151, identity=ident1151, result=res1151,
                   release=res1151.candidates[0], policy=DiscoveryPolicy())


if __name__ == '__main__':
    unittest.main()
