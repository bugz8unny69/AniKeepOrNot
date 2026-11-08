-- v41 -> v42: observational Sonarr backfill results. Never download permits.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v42_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v42_guard SELECT user_version=41 FROM pragma_user_version;
CREATE TABLE sonarr_backfill_results (
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0 AND tvdb_episode<=1000),
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 sonarr_episode_id INTEGER CHECK(sonarr_episode_id>0),
 tvdb_episode_id INTEGER CHECK(tvdb_episode_id>0),
 status TEXT NOT NULL CHECK(status IN ('IN_PROGRESS','NOT_LISTED','IDENTITY_UNKNOWN','IDENTITY_CONFLICT','HAS_FILE','CANDIDATES','NO_RESULTS')),
 candidates INTEGER NOT NULL CHECK(candidates BETWEEN 0 AND 1000),
 approved INTEGER NOT NULL CHECK(approved BETWEEN 0 AND candidates),
 exact INTEGER NOT NULL CHECK(exact BETWEEN 0 AND approved),
 observed_at_ms INTEGER NOT NULL CHECK(observed_at_ms>=0),
 PRIMARY KEY(plan_id,tvdb_episode)
);
INSERT OR ROLLBACK INTO nextgen_v42_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v42_guard;
PRAGMA user_version=42;
COMMIT;
