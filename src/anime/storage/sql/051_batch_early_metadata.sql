-- v50 -> v51: typed observed batch evidence and one-shot preverification admission.
-- Keep existing cryptographic batch claims unchanged. Never synthesize info_sha256.
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v51_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v51_guard SELECT user_version=50 FROM pragma_user_version;
CREATE TABLE batch_verifications_v51 (
 batch_id INTEGER PRIMARY KEY,
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 identity_result_id INTEGER NOT NULL,
 anidb_id INTEGER NOT NULL CHECK(anidb_id>0),
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>0),
 release_sha256 TEXT NOT NULL CHECK(length(release_sha256)=64 AND release_sha256 NOT GLOB '*[^a-f0-9]*'),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^a-f0-9]*'),
 info_sha256 TEXT CHECK(length(info_sha256)=64 AND info_sha256 NOT GLOB '*[^a-f0-9]*'),
 verified_at_ms INTEGER NOT NULL CHECK(verified_at_ms>=0),
 FOREIGN KEY(identity_result_id,anidb_id) REFERENCES identity_results(result_id,anidb_id)
);
INSERT INTO batch_verifications_v51(batch_id,job_id,identity_result_id,anidb_id,tvdb_series_id,tvdb_season,release_sha256,info_hash,info_sha256,verified_at_ms) SELECT batch_id,job_id,identity_result_id,anidb_id,tvdb_series_id,tvdb_season,release_sha256,info_hash,info_sha256,verified_at_ms FROM batch_verifications;
DROP TABLE batch_verifications;
ALTER TABLE batch_verifications_v51 RENAME TO batch_verifications;
CREATE INDEX idx_batch_verifications_identity ON batch_verifications(identity_result_id);
CREATE TABLE batch_torrent_submissions_v51 (
 plan_id INTEGER PRIMARY KEY REFERENCES batch_trial_plans(plan_id),
 reservation_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 first_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 first_attempt INTEGER NOT NULL CHECK(first_attempt>0),
 info_hash TEXT NOT NULL UNIQUE CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^a-f0-9]*'),
 info_sha256 TEXT CHECK(length(info_sha256)=64 AND info_sha256 NOT GLOB '*[^a-f0-9]*'),
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
INSERT INTO batch_torrent_submissions_v51(plan_id,reservation_job_id,first_job_id,first_attempt,info_hash,info_sha256,ownership_marker,base_url,save_path,category,timeout_seconds,api_major,fingerprint,configuration_revision,state,reason_code,created_at_ms,updated_at_ms) SELECT plan_id,reservation_job_id,first_job_id,first_attempt,info_hash,info_sha256,ownership_marker,base_url,save_path,category,timeout_seconds,api_major,fingerprint,configuration_revision,state,reason_code,created_at_ms,updated_at_ms FROM batch_torrent_submissions;
DROP TABLE batch_torrent_submissions;
ALTER TABLE batch_torrent_submissions_v51 RENAME TO batch_torrent_submissions;
CREATE INDEX idx_batch_submission_job ON batch_torrent_submissions(reservation_job_id);
CREATE TABLE batch_metadata_admissions (
 event_id INTEGER PRIMARY KEY REFERENCES incoming_events(event_id),
 verification_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^a-f0-9]*'),
 ownership_marker TEXT NOT NULL UNIQUE CHECK(length(ownership_marker)=32 AND ownership_marker NOT GLOB '*[^a-f0-9]*'),
 base_url TEXT NOT NULL CHECK(length(base_url) BETWEEN 1 AND 2048),
 save_path TEXT NOT NULL CHECK(length(save_path) BETWEEN 2 AND 4096),
 category TEXT NOT NULL CHECK(length(category) BETWEEN 1 AND 64),
 timeout_seconds INTEGER NOT NULL CHECK(timeout_seconds BETWEEN 1 AND 60),
 api_major INTEGER NOT NULL CHECK(api_major IN (4,5)),
 state TEXT NOT NULL CHECK(state IN ('UNCERTAIN','EXTERNAL','REVIEW','OWNED_STOPPED','ADOPTED','RELEASED')),
 inventory_json TEXT CHECK(inventory_json IS NULL OR json_valid(inventory_json)),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms)
);
CREATE UNIQUE INDEX idx_batch_metadata_live_hash ON batch_metadata_admissions(info_hash) WHERE state NOT IN ('RELEASED','EXTERNAL');
INSERT OR ROLLBACK INTO nextgen_v51_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v51_guard;
PRAGMA user_version=51;
COMMIT;
PRAGMA foreign_keys=ON;
