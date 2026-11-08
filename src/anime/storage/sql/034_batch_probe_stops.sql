-- v33 -> v34: one-time probe stop before audio-gated file unlock.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v34_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v34_guard SELECT user_version=33 FROM pragma_user_version;
CREATE TABLE batch_probe_stops (
 plan_id INTEGER PRIMARY KEY REFERENCES batch_probe_media(plan_id),
 stop_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 stop_attempt INTEGER NOT NULL CHECK(stop_attempt>0),
 fingerprint TEXT NOT NULL UNIQUE CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^a-f0-9]*'),
 phase TEXT NOT NULL CHECK(phase IN ('STOP_UNCERTAIN','STOPPED')),
 piece_size INTEGER NOT NULL CHECK(piece_size BETWEEN 1 AND 67108864),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms)
);
INSERT OR ROLLBACK INTO nextgen_v34_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v34_guard;
PRAGMA user_version=34;
COMMIT;
