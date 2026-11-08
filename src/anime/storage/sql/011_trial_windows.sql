-- v10 -> v11: immutable trial-window intents and per-job decisions.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v11_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v11_guard SELECT user_version=10 FROM pragma_user_version;
CREATE TABLE trial_plans (
 plan_id INTEGER PRIMARY KEY,
 anidb_id INTEGER NOT NULL REFERENCES identified_anime(anidb_id),
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>=0),
 tvdb_order TEXT NOT NULL DEFAULT 'official' CHECK(tvdb_order='official'),
 anchor_verification_id INTEGER NOT NULL REFERENCES release_verifications(verification_id),
 anchor_episode INTEGER NOT NULL CHECK(anchor_episode>0),
 backfill_count INTEGER NOT NULL CHECK(backfill_count BETWEEN 0 AND 1000),
 following_count INTEGER NOT NULL CHECK(following_count BETWEEN 0 AND 1000),
 configuration_revision INTEGER NOT NULL CHECK(configuration_revision>0),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 UNIQUE(anidb_id,tvdb_series_id,tvdb_season,tvdb_order),
 CHECK(backfill_count>0 OR following_count>0),
 CHECK(following_count=0 OR anchor_episode<=9223372036854775807-following_count+1)
);
CREATE TABLE trial_evaluations (
 evaluation_id INTEGER PRIMARY KEY,
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 verification_id INTEGER NOT NULL REFERENCES release_verifications(verification_id),
 plan_id INTEGER REFERENCES trial_plans(plan_id),
 selected INTEGER NOT NULL CHECK(selected IN (0,1)),
 reason_code TEXT NOT NULL CHECK(reason_code IN ('TRIAL_SELECTED','TRIAL_OUTSIDE_WINDOW','TRIAL_DISABLED')),
 evaluated_at_ms INTEGER NOT NULL CHECK(evaluated_at_ms>=0),
 CHECK((plan_id IS NULL AND selected=0 AND reason_code='TRIAL_DISABLED') OR
       (plan_id IS NOT NULL AND ((selected=1 AND reason_code='TRIAL_SELECTED') OR
                                (selected=0 AND reason_code='TRIAL_OUTSIDE_WINDOW'))))
);
CREATE INDEX idx_trial_evaluations_plan ON trial_evaluations(plan_id);
DROP TABLE nextgen_v11_guard;
PRAGMA user_version=11;
COMMIT;
