-- v32 -> v33: one-shot audio-gated planned-file unlock and later start.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v33_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v33_guard SELECT user_version=32 FROM pragma_user_version;
CREATE TABLE batch_torrent_unlocks (
 plan_id INTEGER PRIMARY KEY REFERENCES batch_probe_media(plan_id),
 unlock_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 unlock_attempt INTEGER NOT NULL CHECK(unlock_attempt>0),
 verification_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 fingerprint TEXT NOT NULL UNIQUE CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^a-f0-9]*'),
 priorities_json TEXT NOT NULL CHECK(json_valid(priorities_json) AND length(CAST(priorities_json AS BLOB))<=65536),
 piece_size INTEGER NOT NULL CHECK(piece_size BETWEEN 1 AND 67108864),
 phase TEXT NOT NULL CHECK(phase IN ('SELECT_UNCERTAIN','SELECTED','START_UNCERTAIN','ACTIVE','COMPLETE','REVIEW')),
 reason_code TEXT NOT NULL,
 start_job_id INTEGER REFERENCES jobs(job_id),
 start_attempt INTEGER CHECK(start_attempt IS NULL OR start_attempt>0),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms),
 CHECK((start_job_id IS NULL AND start_attempt IS NULL) OR (start_job_id IS NOT NULL AND start_attempt IS NOT NULL)),
 CHECK(phase NOT IN ('START_UNCERTAIN','ACTIVE') OR start_job_id IS NOT NULL),
 CHECK((phase='SELECT_UNCERTAIN' AND reason_code='TORRENT_SELECTION_UNCERTAIN') OR
       (phase='SELECTED' AND reason_code='TORRENT_SELECTION_CONFIRMED') OR
       (phase='START_UNCERTAIN' AND reason_code='TORRENT_START_UNCERTAIN') OR
       (phase='ACTIVE' AND reason_code='TORRENT_DOWNLOAD_IN_PROGRESS') OR
       (phase='COMPLETE' AND reason_code='TORRENT_DOWNLOAD_COMPLETE') OR
       (phase='REVIEW' AND reason_code IN ('TORRENT_TRANSFER_CONFLICT','TORRENT_PIECE_OVERLAP')))
);
INSERT OR ROLLBACK INTO nextgen_v33_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v33_guard;
PRAGMA user_version=33;
COMMIT;
