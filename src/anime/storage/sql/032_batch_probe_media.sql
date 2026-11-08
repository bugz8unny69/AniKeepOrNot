-- v31 -> v32: persistent E01 media/audio proof only, no episode unlock.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v32_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v32_guard SELECT user_version=31 FROM pragma_user_version;
CREATE TABLE batch_probe_media (
 plan_id INTEGER PRIMARY KEY REFERENCES batch_torrent_transfers(plan_id),
 verification_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 transfer_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 submission_fingerprint TEXT NOT NULL CHECK(length(submission_fingerprint)=64 AND submission_fingerprint NOT GLOB '*[^a-f0-9]*'),
 transfer_fingerprint TEXT NOT NULL CHECK(length(transfer_fingerprint)=64 AND transfer_fingerprint NOT GLOB '*[^a-f0-9]*'),
 evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json) AND length(CAST(evidence_json AS BLOB))<=65536),
 verified_at_ms INTEGER NOT NULL CHECK(verified_at_ms>=0)
);
INSERT OR ROLLBACK INTO nextgen_v32_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v32_guard;
PRAGMA user_version=32;
COMMIT;
