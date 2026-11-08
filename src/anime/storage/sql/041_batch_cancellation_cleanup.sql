-- v40 -> v41: distinct cancellation cleanup journal; no additional qB send permits.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v41_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v41_guard SELECT user_version=40 FROM pragma_user_version;
CREATE TABLE batch_cancellation_cleanup (
 plan_id INTEGER PRIMARY KEY REFERENCES batch_torrent_submissions(plan_id),
 operation_id INTEGER NOT NULL REFERENCES job_operations(operation_id),
 fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^a-f0-9]*'),
 files_json TEXT NOT NULL CHECK(json_valid(files_json) AND length(CAST(files_json AS BLOB))<=1048576),
 phase TEXT NOT NULL CHECK(phase IN ('PREPARED','STOP_UNCERTAIN','REMOVE_UNCERTAIN','FILES','COMPLETE')),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms)
);
INSERT OR ROLLBACK INTO nextgen_v41_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v41_guard;
PRAGMA user_version=41;
COMMIT;
