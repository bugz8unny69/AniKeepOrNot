-- v71 -> v72: shared transcode result and durable attempt budget.
BEGIN IMMEDIATE;
CREATE TEMP TABLE anime_v72_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO anime_v72_guard SELECT user_version=71 FROM pragma_user_version;
CREATE TABLE IF NOT EXISTS transcode_media (
 verification_job_id INTEGER PRIMARY KEY REFERENCES jobs(job_id),
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
 retry_history_id INTEGER NOT NULL DEFAULT 0,
 step TEXT NOT NULL DEFAULT 'Waiting for transcoder',
 output_root TEXT,
 evidence_json TEXT CHECK(evidence_json IS NULL OR json_valid(evidence_json))
);
CREATE TABLE IF NOT EXISTS media_probe_details (
 verification_job_id INTEGER PRIMARY KEY REFERENCES jobs(job_id),
 probe_json TEXT NOT NULL CHECK(json_valid(probe_json))
);
DROP TABLE anime_v72_guard;
PRAGMA user_version=72;
COMMIT;
