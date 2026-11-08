-- v21 -> v22: durable Keep import intent and permanent-file result.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v22_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v22_guard SELECT user_version=21 FROM pragma_user_version;
CREATE TABLE keep_imports (
 job_id INTEGER PRIMARY KEY REFERENCES jobs(job_id),
 phase TEXT NOT NULL CHECK(phase IN ('PLANNED','ADD_UNCERTAIN','SERIES_READY','IMPORT_UNCERTAIN','COMPLETE')),
 data_json TEXT NOT NULL CHECK(json_valid(data_json) AND length(CAST(data_json AS BLOB))<=65536),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=0)
);
DROP TABLE nextgen_v22_guard;
PRAGMA user_version=22;
COMMIT;
