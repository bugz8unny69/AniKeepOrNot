-- v12 -> v13: metadata-bound preparation, not external submission/ownership.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v13_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v13_guard SELECT user_version=12 FROM pragma_user_version;
CREATE TABLE acquisition_plans (
 plan_id INTEGER PRIMARY KEY,
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 reservation_id INTEGER NOT NULL UNIQUE REFERENCES episode_reservations(reservation_id),
 info_hash TEXT NOT NULL UNIQUE CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^a-f0-9]*'),
 info_sha256 TEXT NOT NULL CHECK(length(info_sha256)=64 AND info_sha256 NOT GLOB '*[^a-f0-9]*'),
 manifest_json TEXT NOT NULL CHECK(length(CAST(manifest_json AS BLOB)) BETWEEN 1 AND 1048576),
 submission_marker TEXT NOT NULL UNIQUE CHECK(length(submission_marker)=32 AND submission_marker NOT GLOB '*[^a-f0-9]*'),
 state TEXT NOT NULL DEFAULT 'PREPARED' CHECK(state='PREPARED'),
 configuration_revision INTEGER NOT NULL CHECK(configuration_revision>0),
 prepared_at_ms INTEGER NOT NULL CHECK(prepared_at_ms>=0)
);
DROP TABLE nextgen_v13_guard;
PRAGMA user_version=13;
COMMIT;
