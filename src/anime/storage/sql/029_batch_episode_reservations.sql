-- v28 -> v29: batch episode reservations; no external torrent operation.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v29_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v29_guard SELECT user_version=28 FROM pragma_user_version;
CREATE TABLE batch_episode_reservations (
 reservation_id INTEGER PRIMARY KEY,
 plan_id INTEGER NOT NULL REFERENCES batch_trial_plans(plan_id),
 owner_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 file_index INTEGER NOT NULL CHECK(file_index>=0),
 anidb_id INTEGER NOT NULL REFERENCES identified_anime(anidb_id),
 anidb_episode_id INTEGER NOT NULL CHECK(anidb_episode_id>0),
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>0),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0),
 tvdb_episode_id INTEGER NOT NULL CHECK(tvdb_episode_id>0),
 state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(state IN ('ACTIVE','RELEASED')),
 reserved_at_ms INTEGER NOT NULL CHECK(reserved_at_ms>=0),
 ended_at_ms INTEGER,
 ended_by_job_id INTEGER REFERENCES jobs(job_id),
 UNIQUE(plan_id,file_index),
 UNIQUE(plan_id,tvdb_episode),
 FOREIGN KEY(plan_id,file_index) REFERENCES batch_trial_files(plan_id,file_index),
 CHECK((state='ACTIVE' AND ended_at_ms IS NULL AND ended_by_job_id IS NULL) OR
       (state='RELEASED' AND ended_at_ms>=reserved_at_ms AND ended_by_job_id IS NOT NULL))
);
CREATE UNIQUE INDEX idx_batch_active_anidb ON batch_episode_reservations(anidb_episode_id) WHERE state='ACTIVE';
CREATE UNIQUE INDEX idx_batch_active_tvdb ON batch_episode_reservations(tvdb_episode_id) WHERE state='ACTIVE';
CREATE UNIQUE INDEX idx_batch_active_placement ON batch_episode_reservations(tvdb_series_id,tvdb_season,tvdb_episode) WHERE state='ACTIVE';
CREATE INDEX idx_batch_owner ON batch_episode_reservations(owner_job_id,state);
INSERT OR ROLLBACK INTO nextgen_v29_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v29_guard;
PRAGMA user_version=29;
COMMIT;
