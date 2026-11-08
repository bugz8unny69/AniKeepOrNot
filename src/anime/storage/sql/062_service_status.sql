-- v61 -> v62: last known status of each external service, written by the
-- worker and read by the API (the dashboard status bar and the Keep check).
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v62_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v62_guard SELECT user_version=61 FROM pragma_user_version;
CREATE TABLE service_status (
 service TEXT PRIMARY KEY CHECK(length(service) BETWEEN 1 AND 64),
 label TEXT NOT NULL CHECK(length(label) BETWEEN 1 AND 64),
 status TEXT NOT NULL CHECK(status IN ('UP','DOWN','NOT_CONFIGURED','AUTH_FAILED','UNSUPPORTED','DISABLED','UNKNOWN')),
 detail TEXT NOT NULL DEFAULT '' CHECK(length(detail) <= 500),
 since_ms INTEGER NOT NULL CHECK(since_ms >= 0),
 checked_at_ms INTEGER NOT NULL CHECK(checked_at_ms >= 0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= 0)
);
DROP TABLE nextgen_v62_guard;
PRAGMA user_version=62;
COMMIT;
