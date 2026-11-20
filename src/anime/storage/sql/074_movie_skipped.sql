-- v73 -> v74: a movie release is skipped with a MOVIE_SKIPPED notification.
BEGIN IMMEDIATE;
CREATE TEMP TABLE anime_v74_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO anime_v74_guard SELECT user_version=73 FROM pragma_user_version;
DROP INDEX idx_notifications_group;
ALTER TABLE notifications RENAME TO old_notifications;
CREATE TABLE notifications (
 notification_id INTEGER PRIMARY KEY,
 type TEXT NOT NULL CHECK(type IN ('API_REJECTION','SERIES_EPISODE_TROUBLE','KEEP_SEARCH_FAILED','TRANSCODE_BATCH_DONE','MOVIE_SKIPPED')),
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
DROP TABLE anime_v74_guard;
PRAGMA user_version=74;
COMMIT;
