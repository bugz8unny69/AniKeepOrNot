-- v47 -> v48: Prowlarr backfill per-episode statuses. Historical Sonarr rows are preserved.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v48_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v48_guard SELECT user_version=47 FROM pragma_user_version;
CREATE TABLE sonarr_backfill_results_v48 (
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0 AND tvdb_episode<=1000),
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 sonarr_episode_id INTEGER CHECK(sonarr_episode_id>0),
 tvdb_episode_id INTEGER CHECK(tvdb_episode_id>0),
 status TEXT NOT NULL CHECK(status IN ('IN_PROGRESS','NOT_LISTED','IDENTITY_UNKNOWN','IDENTITY_CONFLICT','HAS_FILE','CANDIDATES','NO_RESULTS','NOT_AIRED','MAPPING_MISSING')),
 candidates INTEGER NOT NULL CHECK(candidates BETWEEN 0 AND 1000),
 approved INTEGER NOT NULL CHECK(approved BETWEEN 0 AND candidates),
 exact INTEGER NOT NULL CHECK(exact BETWEEN 0 AND candidates),
 observed_at_ms INTEGER NOT NULL CHECK(observed_at_ms>=0),
 PRIMARY KEY(plan_id,tvdb_episode)
);
INSERT INTO sonarr_backfill_results_v48 SELECT * FROM sonarr_backfill_results;
DROP TABLE sonarr_backfill_results;
ALTER TABLE sonarr_backfill_results_v48 RENAME TO sonarr_backfill_results;
INSERT OR ROLLBACK INTO nextgen_v48_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v48_guard;
PRAGMA user_version=48;
COMMIT;
