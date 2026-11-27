"""Small SQLite tests for the immutable backfill selection receipt."""
import sqlite3
import unittest

from src.anime.acquisition.prowlarr_search import DiscoveryPolicy, Release, SearchResult
from src.anime.episode_identity import VerifiedEpisode

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
            CREATE TABLE user_evaluation_plans (
              plan_id INTEGER PRIMARY KEY, anidb_id INTEGER,
              tvdb_series_id INTEGER, tvdb_season INTEGER, tvdb_order TEXT,
              anchor_verification_id INTEGER,
              anchor_episode INTEGER, backfill_count INTEGER, following_count INTEGER);
            CREATE TABLE user_evaluation_checks (
              evaluation_id INTEGER PRIMARY KEY, job_id INTEGER,
              verification_id INTEGER, plan_id INTEGER);
            CREATE TABLE job_history (
              history_id INTEGER PRIMARY KEY, job_id INTEGER, event_code TEXT);
            CREATE TABLE episode_discovery_results (
              plan_id INTEGER, anidb_episode_number INTEGER, PRIMARY KEY(plan_id, anidb_episode_number));
            CREATE TABLE recognized_episodes (
              anidb_id INTEGER, tvdb_series_id INTEGER, tvdb_season INTEGER,
              tvdb_episode INTEGER, tvdb_episode_id INTEGER, anidb_episode_number INTEGER);
            CREATE TABLE episode_reservations (
              state TEXT, tvdb_series_id INTEGER, tvdb_season INTEGER, tvdb_episode INTEGER);
            CREATE TABLE batch_episode_reservations (
              state TEXT, tvdb_series_id INTEGER, tvdb_season INTEGER, tvdb_episode INTEGER);
            CREATE TABLE episode_release_selections (
              plan_id INTEGER, anidb_episode_number INTEGER, tvdb_season INTEGER,
              tvdb_episode INTEGER, tvdb_episode_id INTEGER,
              info_hash TEXT, selected_at_ms INTEGER,
              PRIMARY KEY(plan_id,anidb_episode_number), UNIQUE(plan_id,info_hash));
            INSERT INTO user_evaluation_plans VALUES (1,50,100,1,'official',10,10,6,1);
            INSERT INTO user_evaluation_checks VALUES (1,1,10,1);
            INSERT INTO job_history VALUES (1,1,'USER_EVALUATION_AUDIO_SEARCH');
            INSERT INTO recognized_episodes VALUES (50,100,1,10,200,10);
        """)
        self.db.commit()











if __name__ == '__main__':
    unittest.main()
