-- v36 -> v37: one persistent Sonarr import state per kept batch episode.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v37_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v37_guard SELECT user_version=36 FROM pragma_user_version;
CREATE TABLE batch_keep_imports (
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 plan_id INTEGER NOT NULL,
 file_index INTEGER NOT NULL,
 phase TEXT NOT NULL CHECK(phase IN ('PLANNED','ADD_UNCERTAIN','SERIES_READY','IMPORT_UNCERTAIN','COMPLETE')),
 data_json TEXT NOT NULL CHECK(json_valid(data_json) AND length(CAST(data_json AS BLOB)) BETWEEN 1 AND 65536),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=0),
 PRIMARY KEY(job_id,file_index),
 UNIQUE(job_id,plan_id,file_index),
 FOREIGN KEY(plan_id,file_index) REFERENCES batch_staging_publications(plan_id,file_index)
);
INSERT OR ROLLBACK INTO nextgen_v37_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v37_guard;
PRAGMA user_version=37;
COMMIT;
