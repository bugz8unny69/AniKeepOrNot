-- v11 -> v12: exact-episode reservations, not external download ownership.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v12_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v12_guard SELECT user_version=11 FROM pragma_user_version;
CREATE TABLE episode_reservations (
 reservation_id INTEGER PRIMARY KEY,
 owner_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 evaluation_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 verification_id INTEGER NOT NULL REFERENCES release_verifications(verification_id),
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 anidb_id INTEGER NOT NULL REFERENCES identified_anime(anidb_id),
 anidb_episode_id INTEGER NOT NULL UNIQUE REFERENCES identified_episodes(anidb_episode_id),
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>=0),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0),
 tvdb_episode_id INTEGER NOT NULL UNIQUE CHECK(tvdb_episode_id>0),
 tvdb_order TEXT NOT NULL DEFAULT 'official' CHECK(tvdb_order='official'),
 reserved_at_ms INTEGER NOT NULL CHECK(reserved_at_ms>=0),
 UNIQUE(tvdb_series_id,tvdb_season,tvdb_episode,tvdb_order)
);
DROP TABLE nextgen_v12_guard;
PRAGMA user_version=12;
COMMIT;
