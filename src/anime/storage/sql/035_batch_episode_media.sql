-- v34 -> v35: per-episode JP+EN file evidence
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v35_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v35_guard SELECT user_version=34 FROM pragma_user_version;
CREATE TABLE batch_episode_media (
 plan_id INTEGER NOT NULL,
 file_index INTEGER NOT NULL,
 verification_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 unlock_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 submission_fingerprint TEXT NOT NULL CHECK(length(submission_fingerprint)=64),
 transfer_fingerprint TEXT NOT NULL CHECK(length(transfer_fingerprint)=64),
 evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json) AND length(CAST(evidence_json AS BLOB))<=65536),
 verified_at_ms INTEGER NOT NULL CHECK(verified_at_ms>=0),
 PRIMARY KEY(plan_id,file_index),
 FOREIGN KEY(plan_id,file_index) REFERENCES batch_trial_files(plan_id,file_index),
 FOREIGN KEY(plan_id) REFERENCES batch_torrent_unlocks(plan_id)
);
INSERT OR ROLLBACK INTO nextgen_v35_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v35_guard;
PRAGMA user_version=35;
COMMIT;
