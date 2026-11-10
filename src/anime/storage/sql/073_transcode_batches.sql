-- v72 -> v73: one resumable staged-transcode batch and its completion event.
BEGIN IMMEDIATE;
CREATE TEMP TABLE anime_v73_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO anime_v73_guard SELECT user_version=72 FROM pragma_user_version;
CREATE TABLE IF NOT EXISTS transcode_batches (
 batch_id INTEGER PRIMARY KEY,
 anidb_id INTEGER CHECK(anidb_id IS NULL OR anidb_id>0),
 state TEXT NOT NULL CHECK(state IN ('QUEUED','RUNNING','DONE')),
 total INTEGER NOT NULL CHECK(total>=0),
 done INTEGER NOT NULL DEFAULT 0 CHECK(done>=0),
 skipped INTEGER NOT NULL DEFAULT 0 CHECK(skipped>=0),
 failed INTEGER NOT NULL DEFAULT 0 CHECK(failed>=0),
 failed_files TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(failed_files)),
 current_file TEXT,
 created_at_ms INTEGER NOT NULL,
 finished_at_ms INTEGER,
 progress_json TEXT NOT NULL CHECK(json_valid(progress_json))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_transcode_batch ON transcode_batches((1)) WHERE state IN ('QUEUED','RUNNING');
DROP INDEX idx_notifications_group;
ALTER TABLE notifications RENAME TO old_notifications;
CREATE TABLE notifications (
 notification_id INTEGER PRIMARY KEY,
 type TEXT NOT NULL CHECK(type IN ('API_REJECTION','SERIES_EPISODE_TROUBLE','KEEP_SEARCH_FAILED','TRANSCODE_BATCH_DONE')),
 severity TEXT NOT NULL CHECK(severity IN ('info','attention')),
 title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 120),
 message TEXT NOT NULL CHECK(length(message) BETWEEN 1 AND 500),
 anidb_id INTEGER CHECK(anidb_id IS NULL OR anidb_id>0),
 job_id INTEGER CHECK(job_id IS NULL OR job_id>0),
 release_name TEXT CHECK(release_name IS NULL OR length(release_name) BETWEEN 1 AND 1024),
 group_key TEXT CHECK(group_key IS NULL OR length(group_key) BETWEEN 1 AND 200),
 raw_body BLOB CHECK(raw_body IS NULL OR length(raw_body)<=65536),
 count INTEGER NOT NULL CHECK(count>=1),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 last_at_ms INTEGER NOT NULL CHECK(last_at_ms>=created_at_ms)
);
INSERT INTO notifications SELECT * FROM old_notifications;
DROP TABLE old_notifications;
CREATE UNIQUE INDEX idx_notifications_group ON notifications(group_key) WHERE group_key IS NOT NULL;
DROP TABLE anime_v73_guard;
PRAGMA user_version=73;
COMMIT;
