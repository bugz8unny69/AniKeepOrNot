-- v19 -> v20: pinned recoverable staging publication plans.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v20_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v20_guard SELECT user_version=19 FROM pragma_user_version;
CREATE TABLE IF NOT EXISTS staging_publications (
 plan_id INTEGER PRIMARY KEY REFERENCES media_verifications(plan_id),
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 verification_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json) AND length(CAST(manifest_json AS BLOB)) BETWEEN 1 AND 65536),
 state TEXT NOT NULL CHECK(state IN ('PLANNED','PUBLISHED')),
 receipts_json TEXT CHECK(receipts_json IS NULL OR (json_valid(receipts_json) AND length(CAST(receipts_json AS BLOB))<=16384)),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 published_at_ms INTEGER CHECK(published_at_ms IS NULL OR published_at_ms>=created_at_ms),
 CHECK(job_id<>verification_job_id),
 CHECK((state='PLANNED' AND receipts_json IS NULL AND published_at_ms IS NULL) OR
       (state='PUBLISHED' AND receipts_json IS NOT NULL AND published_at_ms IS NOT NULL))
);
DROP TABLE nextgen_v20_guard;
PRAGMA user_version=20;
COMMIT;
