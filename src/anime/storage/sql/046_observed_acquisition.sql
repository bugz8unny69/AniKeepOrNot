-- v45 -> v46: explicitly distinguish observed inventory from metainfo evidence.
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v46_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v46_guard SELECT user_version=45 FROM pragma_user_version;
CREATE TABLE acquisition_plans_next (
 plan_id INTEGER PRIMARY KEY,
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 reservation_id INTEGER NOT NULL UNIQUE REFERENCES episode_reservations(reservation_id),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^a-f0-9]*'),
 info_sha256 TEXT CHECK(length(info_sha256)=64 AND info_sha256 NOT GLOB '*[^a-f0-9]*'),
 manifest_json TEXT NOT NULL CHECK(length(CAST(manifest_json AS BLOB)) BETWEEN 1 AND 1048576),
 submission_marker TEXT NOT NULL UNIQUE CHECK(length(submission_marker)=32 AND submission_marker NOT GLOB '*[^a-f0-9]*'),
 state TEXT NOT NULL DEFAULT 'PREPARED' CHECK(state IN ('PREPARED','RELEASED')),
 configuration_revision INTEGER NOT NULL CHECK(configuration_revision>0),
 prepared_at_ms INTEGER NOT NULL CHECK(prepared_at_ms>=0),
 selected_size_bytes INTEGER CHECK(selected_size_bytes IS NULL OR selected_size_bytes>0)
);
INSERT INTO acquisition_plans_next SELECT * FROM acquisition_plans;
DROP TABLE acquisition_plans;
ALTER TABLE acquisition_plans_next RENAME TO acquisition_plans;
CREATE UNIQUE INDEX IF NOT EXISTS idx_acquisition_active_hash ON acquisition_plans(info_hash) WHERE state='PREPARED';
INSERT OR ROLLBACK INTO nextgen_v46_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v46_guard;
PRAGMA user_version=46;
COMMIT;
PRAGMA foreign_keys=ON;
