-- v53 -> v54: durable search snapshots for explainable Prowlarr matching diagnostics.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v54_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v54_guard SELECT user_version=53 FROM pragma_user_version;
CREATE TABLE prowlarr_search_snapshots (
 snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 attempt INTEGER NOT NULL CHECK(attempt >= 0),
 captured_at_ms INTEGER NOT NULL CHECK(captured_at_ms >= 0),
 plan_id INTEGER CHECK(plan_id IS NULL OR plan_id > 0),
 tvdb_series_id INTEGER CHECK(tvdb_series_id IS NULL OR tvdb_series_id > 0),
 season INTEGER CHECK(season IS NULL OR season >= 0),
 episode INTEGER CHECK(episode IS NULL OR episode > 0),
 search_mode TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('COMPLETE', 'PARTIAL', 'ERROR')),
 snapshot_json TEXT NOT NULL
);
CREATE INDEX idx_prowlarr_snapshots_job ON prowlarr_search_snapshots(job_id, attempt);
CREATE INDEX idx_prowlarr_snapshots_plan ON prowlarr_search_snapshots(plan_id, episode);
INSERT OR ROLLBACK INTO nextgen_v54_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v54_guard;
PRAGMA user_version=54;
COMMIT;
