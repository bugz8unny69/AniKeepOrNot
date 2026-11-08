-- NextGen schema v4: history emitter attribution; v3 -> v4 only.
-- No legacy databases. Standalone script, no existing transaction.
-- The guard and both ALTERs/version bump share one transaction. The caller
-- must ROLLBACK if executescript raises; a future runner owns that handling.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v4_guard(version INTEGER CHECK(version=3));
INSERT OR ROLLBACK INTO nextgen_v4_guard SELECT user_version FROM pragma_user_version;
DROP TABLE nextgen_v4_guard;
ALTER TABLE event_history ADD COLUMN component TEXT
 CHECK(component IS NULL OR component IN ('supervisor','intake','coordinator','identity','acquisition'));
ALTER TABLE job_history ADD COLUMN component TEXT
 CHECK(component IS NULL OR component IN ('supervisor','intake','coordinator','identity','acquisition'));
PRAGMA user_version=4;
COMMIT;
