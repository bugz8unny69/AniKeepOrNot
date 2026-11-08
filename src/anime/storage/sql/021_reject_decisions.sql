-- v20 -> v21: explicit anime rejection and queued selected-file cleanup.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v21_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v21_guard SELECT user_version=20 FROM pragma_user_version;
ALTER TABLE management_actions RENAME TO management_actions_v20;
CREATE TABLE management_actions (
 operation_id INTEGER NOT NULL REFERENCES job_operations(operation_id),
 action TEXT NOT NULL CHECK(action IN ('KEEP','CLEANUP','REJECT')),
 target_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 PRIMARY KEY(operation_id,action),
 CHECK(target_job_id<>job_id)
);
INSERT INTO management_actions SELECT * FROM management_actions_v20;
DROP TABLE management_actions_v20;
CREATE TABLE IF NOT EXISTS anime_review_decisions (
 anidb_id INTEGER PRIMARY KEY REFERENCES identified_anime(anidb_id),
 operation_id INTEGER NOT NULL REFERENCES job_operations(operation_id),
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 decision TEXT NOT NULL CHECK(decision='REJECT'),
 cleanup_state TEXT NOT NULL CHECK(cleanup_state IN ('PENDING','COMPLETE')),
 decided_at_ms INTEGER NOT NULL CHECK(decided_at_ms>=0)
);
DROP TABLE nextgen_v21_guard;
PRAGMA user_version=21;
COMMIT;
