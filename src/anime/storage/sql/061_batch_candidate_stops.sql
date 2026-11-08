-- v60 -> v61: why each batch candidate stopped, recorded when it is decided.
-- A user Cancel records CANCELED; a failing job records FAILED with its own
-- reason code. The controller applies it once cleanup has completed.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v61_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v61_guard SELECT user_version=60 FROM pragma_user_version;
CREATE TABLE batch_candidate_stops (
 candidate_id INTEGER PRIMARY KEY REFERENCES batch_candidates(candidate_id),
 operation_id INTEGER NOT NULL REFERENCES job_operations(operation_id),
 disposition TEXT NOT NULL CHECK(disposition IN ('FAILED','CANCELED')),
 reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 1 AND 64),
 recorded_at_ms INTEGER NOT NULL CHECK(recorded_at_ms >= 0)
);
CREATE INDEX idx_batch_candidate_stops_operation ON batch_candidate_stops(operation_id);
INSERT OR ROLLBACK INTO nextgen_v61_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v61_guard;
PRAGMA user_version=61;
COMMIT;
