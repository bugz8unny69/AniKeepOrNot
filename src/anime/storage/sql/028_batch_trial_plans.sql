-- v27 -> v28: read-only immutable batch trial plans; NOT reservations.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v28_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v28_guard SELECT user_version=27 FROM pragma_user_version;
CREATE TABLE batch_trial_plans (
 plan_id INTEGER PRIMARY KEY,
 batch_id INTEGER NOT NULL UNIQUE REFERENCES batch_verifications(batch_id),
 evaluation_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 anchor_episode INTEGER NOT NULL CHECK(anchor_episode>0),
 backfill_count INTEGER NOT NULL CHECK(backfill_count BETWEEN 0 AND 1000),
 following_count INTEGER NOT NULL CHECK(following_count BETWEEN 0 AND 1000),
 configuration_revision INTEGER NOT NULL CHECK(configuration_revision>0),
 max_episode_bytes INTEGER NOT NULL CHECK(max_episode_bytes>0),
 max_plan_bytes INTEGER NOT NULL CHECK(max_plan_bytes>0),
 planned_size_bytes INTEGER NOT NULL CHECK(planned_size_bytes>0 AND planned_size_bytes<=max_plan_bytes),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 CHECK(backfill_count>0 OR following_count>0)
);
CREATE TABLE batch_trial_files (
 plan_id INTEGER NOT NULL REFERENCES batch_trial_plans(plan_id),
 file_index INTEGER NOT NULL CHECK(file_index>=0),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0),
 selected_size_bytes INTEGER NOT NULL CHECK(selected_size_bytes>0),
 PRIMARY KEY(plan_id,file_index),
 UNIQUE(plan_id,tvdb_episode)
);
CREATE INDEX idx_batch_trial_by_episode ON batch_trial_files(plan_id,tvdb_episode);
INSERT OR ROLLBACK INTO nextgen_v28_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v28_guard;
PRAGMA user_version=28;
COMMIT;
