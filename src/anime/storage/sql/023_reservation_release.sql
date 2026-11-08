-- v22 -> v23: retain historical reservations/plans while freeing cleaned canceled slots.
-- The maintenance runner validates foreign keys and restores enforcement on failure.
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v23_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v23_guard SELECT user_version=22 FROM pragma_user_version;
CREATE TABLE episode_reservations_next (
 reservation_id INTEGER PRIMARY KEY,
 owner_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 evaluation_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 verification_id INTEGER NOT NULL REFERENCES release_verifications(verification_id),
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 anidb_id INTEGER NOT NULL REFERENCES identified_anime(anidb_id),
 anidb_episode_id INTEGER NOT NULL REFERENCES identified_episodes(anidb_episode_id),
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>=0),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0),
 tvdb_episode_id INTEGER NOT NULL CHECK(tvdb_episode_id>0),
 tvdb_order TEXT NOT NULL DEFAULT 'official' CHECK(tvdb_order='official'),
 reserved_at_ms INTEGER NOT NULL CHECK(reserved_at_ms>=0),
 state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(state IN ('ACTIVE','RELEASED')),
 ended_at_ms INTEGER CHECK(ended_at_ms IS NULL OR ended_at_ms>=0),
 ended_by_job_id INTEGER REFERENCES jobs(job_id),
 CHECK((state='ACTIVE' AND ended_at_ms IS NULL AND ended_by_job_id IS NULL) OR
       (state='RELEASED' AND ended_at_ms IS NOT NULL AND ended_by_job_id IS NOT NULL))
);
INSERT INTO episode_reservations_next(reservation_id,owner_job_id,evaluation_job_id,verification_id,plan_id,anidb_id,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id,tvdb_order,reserved_at_ms) SELECT reservation_id,owner_job_id,evaluation_job_id,verification_id,plan_id,anidb_id,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,tvdb_episode_id,tvdb_order,reserved_at_ms FROM episode_reservations;
DROP TABLE episode_reservations;
ALTER TABLE episode_reservations_next RENAME TO episode_reservations;
CREATE UNIQUE INDEX IF NOT EXISTS idx_episode_active_anidb ON episode_reservations(anidb_episode_id) WHERE state='ACTIVE';
CREATE UNIQUE INDEX IF NOT EXISTS idx_episode_active_tvdb ON episode_reservations(tvdb_episode_id) WHERE state='ACTIVE';
CREATE UNIQUE INDEX IF NOT EXISTS idx_episode_active_placement ON episode_reservations(tvdb_series_id,tvdb_season,tvdb_episode,tvdb_order) WHERE state='ACTIVE';
CREATE TABLE acquisition_plans_next (
 plan_id INTEGER PRIMARY KEY,
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 reservation_id INTEGER NOT NULL UNIQUE REFERENCES episode_reservations(reservation_id),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^a-f0-9]*'),
 info_sha256 TEXT NOT NULL CHECK(length(info_sha256)=64 AND info_sha256 NOT GLOB '*[^a-f0-9]*'),
 manifest_json TEXT NOT NULL CHECK(length(CAST(manifest_json AS BLOB)) BETWEEN 1 AND 1048576),
 submission_marker TEXT NOT NULL UNIQUE CHECK(length(submission_marker)=32 AND submission_marker NOT GLOB '*[^a-f0-9]*'),
 state TEXT NOT NULL DEFAULT 'PREPARED' CHECK(state IN ('PREPARED','RELEASED')),
 configuration_revision INTEGER NOT NULL CHECK(configuration_revision>0),
 prepared_at_ms INTEGER NOT NULL CHECK(prepared_at_ms>=0),
 selected_size_bytes INTEGER CHECK(selected_size_bytes IS NULL OR selected_size_bytes>0)
);
INSERT INTO acquisition_plans_next(plan_id,job_id,reservation_id,info_hash,info_sha256,manifest_json,submission_marker,state,configuration_revision,prepared_at_ms,selected_size_bytes) SELECT plan_id,job_id,reservation_id,info_hash,info_sha256,manifest_json,submission_marker,state,configuration_revision,prepared_at_ms,selected_size_bytes FROM acquisition_plans;
DROP TABLE acquisition_plans;
ALTER TABLE acquisition_plans_next RENAME TO acquisition_plans;
CREATE UNIQUE INDEX IF NOT EXISTS idx_acquisition_active_hash ON acquisition_plans(info_hash) WHERE state='PREPARED';
DROP TABLE nextgen_v23_guard;
PRAGMA user_version=23;
COMMIT;
PRAGMA foreign_keys=ON;
