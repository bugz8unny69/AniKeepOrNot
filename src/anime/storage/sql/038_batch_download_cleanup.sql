-- v37 -> v38: batch exact-manifest, one-shot owned torrent cleanup.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v38_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v38_guard SELECT user_version=37 FROM pragma_user_version;
CREATE TABLE batch_download_cleanup (
 plan_id INTEGER PRIMARY KEY REFERENCES batch_torrent_submissions(plan_id),
 decision_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 operation_id INTEGER NOT NULL REFERENCES job_operations(operation_id),
 fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^a-f0-9]*'),
 files_json TEXT NOT NULL CHECK(json_valid(files_json) AND length(CAST(files_json AS BLOB))<=1048576),
 phase TEXT NOT NULL CHECK(phase IN ('PREPARED','STOP_UNCERTAIN','REMOVE_UNCERTAIN','FILES','COMPLETE')),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms)
);
INSERT OR ROLLBACK INTO nextgen_v38_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v38_guard;
PRAGMA user_version=38;
COMMIT;
