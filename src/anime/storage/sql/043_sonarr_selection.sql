-- v42 -> v43: independent Sonarr/local candidate counts and a single verified selection per episode.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v43_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v43_guard SELECT user_version=42 FROM pragma_user_version;
CREATE TABLE sonarr_backfill_results_v43 (
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0 AND tvdb_episode<=1000),
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 sonarr_episode_id INTEGER CHECK(sonarr_episode_id>0),
 tvdb_episode_id INTEGER CHECK(tvdb_episode_id>0),
 status TEXT NOT NULL CHECK(status IN ('IN_PROGRESS','NOT_LISTED','IDENTITY_UNKNOWN','IDENTITY_CONFLICT','HAS_FILE','CANDIDATES','NO_RESULTS')),
 candidates INTEGER NOT NULL CHECK(candidates BETWEEN 0 AND 1000),
 approved INTEGER NOT NULL CHECK(approved BETWEEN 0 AND candidates),
 exact INTEGER NOT NULL CHECK(exact BETWEEN 0 AND candidates),
 observed_at_ms INTEGER NOT NULL CHECK(observed_at_ms>=0),
 PRIMARY KEY(plan_id,tvdb_episode)
);
INSERT INTO sonarr_backfill_results_v43 SELECT * FROM sonarr_backfill_results;
DROP TABLE sonarr_backfill_results;
ALTER TABLE sonarr_backfill_results_v43 RENAME TO sonarr_backfill_results;
-- A selected hash is a search hint, NOT an acquisition plan, download permit or reservation.
CREATE TABLE sonarr_episode_selections (
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0),
 tvdb_episode_id INTEGER NOT NULL CHECK(tvdb_episode_id>0),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^0-9a-f]*'),
 selected_at_ms INTEGER NOT NULL CHECK(selected_at_ms>=0),
 PRIMARY KEY(plan_id,tvdb_episode),
 UNIQUE(plan_id,info_hash)
);
INSERT OR ROLLBACK INTO nextgen_v43_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v43_guard;
PRAGMA user_version=43;
COMMIT;
