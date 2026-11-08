-- v29 -> v30: one-shot batch stopped-torrent intent (no automatic send).
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v30_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v30_guard SELECT user_version=29 FROM pragma_user_version;
CREATE TABLE batch_torrent_submissions (
 plan_id INTEGER PRIMARY KEY REFERENCES batch_trial_plans(plan_id),
 reservation_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 first_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 first_attempt INTEGER NOT NULL CHECK(first_attempt>0),
 info_hash TEXT NOT NULL UNIQUE CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^a-f0-9]*'),
 info_sha256 TEXT NOT NULL CHECK(length(info_sha256)=64 AND info_sha256 NOT GLOB '*[^a-f0-9]*'),
 ownership_marker TEXT NOT NULL UNIQUE CHECK(length(ownership_marker)=32 AND ownership_marker NOT GLOB '*[^a-f0-9]*'),
 base_url TEXT NOT NULL CHECK(length(base_url) BETWEEN 1 AND 2048),
 save_path TEXT NOT NULL CHECK(length(save_path) BETWEEN 2 AND 4096),
 category TEXT NOT NULL CHECK(length(category) BETWEEN 1 AND 64),
 timeout_seconds INTEGER NOT NULL CHECK(timeout_seconds BETWEEN 1 AND 60),
 api_major INTEGER NOT NULL CHECK(api_major IN (4,5)),
 fingerprint TEXT NOT NULL UNIQUE CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^a-f0-9]*'),
 configuration_revision INTEGER NOT NULL CHECK(configuration_revision>0),
 state TEXT NOT NULL CHECK(state IN ('UNCERTAIN','EXTERNAL','REVIEW','OWNED_STOPPED')),
 reason_code TEXT NOT NULL CHECK(
   (state='UNCERTAIN' AND reason_code='TORRENT_SUBMISSION_UNCERTAIN') OR
   (state='EXTERNAL' AND reason_code='TORRENT_ALREADY_PRESENT') OR
   (state='REVIEW' AND reason_code='TORRENT_PROVENANCE_CONFLICT') OR
   (state='OWNED_STOPPED' AND reason_code='TORRENT_STOPPED_CONFIRMED')),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms)
);
CREATE INDEX idx_batch_submission_job ON batch_torrent_submissions(reservation_job_id);
INSERT OR ROLLBACK INTO nextgen_v30_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v30_guard;
PRAGMA user_version=30;
COMMIT;
