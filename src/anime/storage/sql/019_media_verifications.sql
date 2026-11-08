-- v18 -> v19: immutable verified selected-video evidence.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v19_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v19_guard SELECT user_version=18 FROM pragma_user_version;
CREATE TABLE IF NOT EXISTS media_verifications (
 plan_id INTEGER PRIMARY KEY REFERENCES acquisition_plans(plan_id),
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 download_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 submission_fingerprint TEXT NOT NULL CHECK(length(submission_fingerprint)=64 AND submission_fingerprint NOT GLOB '*[^a-f0-9]*'),
 transfer_fingerprint TEXT NOT NULL CHECK(length(transfer_fingerprint)=64 AND transfer_fingerprint NOT GLOB '*[^a-f0-9]*'),
 evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json) AND length(CAST(evidence_json AS BLOB)) BETWEEN 1 AND 16384),
 verified_at_ms INTEGER NOT NULL CHECK(verified_at_ms>=0),
 CHECK(job_id<>download_job_id)
);
DROP TABLE nextgen_v19_guard;
PRAGMA user_version=19;
COMMIT;
