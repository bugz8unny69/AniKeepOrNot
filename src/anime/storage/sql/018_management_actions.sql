-- v17 -> v18: durable, deduplicated management control jobs.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v18_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v18_guard SELECT user_version=17 FROM pragma_user_version;
CREATE TABLE management_actions (
 operation_id INTEGER NOT NULL REFERENCES job_operations(operation_id),
 action TEXT NOT NULL CHECK(action IN ('KEEP','CLEANUP')),
 target_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 PRIMARY KEY(operation_id,action),
 CHECK(target_job_id<>job_id)
);
DROP TABLE nextgen_v18_guard;
PRAGMA user_version=18;
COMMIT;
