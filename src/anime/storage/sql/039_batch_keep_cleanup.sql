-- v38 -> v39: batch post-Keep exact staging-file cleanup.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v39_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v39_guard SELECT user_version=38 FROM pragma_user_version;
CREATE TABLE batch_keep_cleanup (
 job_id INTEGER PRIMARY KEY REFERENCES jobs(job_id),
 decision_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 phase TEXT NOT NULL CHECK(phase IN ('PENDING','COMPLETE')),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms)
);
INSERT OR ROLLBACK INTO nextgen_v39_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v39_guard;
PRAGMA user_version=39;
COMMIT;
