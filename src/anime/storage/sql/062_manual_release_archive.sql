-- v61 -> v62: retain audit links after safely retired manual candidates.
-- Raw uploaded torrent bytes are deliberately not copied to the archive.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v62_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v62_guard SELECT user_version=61 FROM pragma_user_version;
CREATE TABLE manual_release_archive (
 event_id INTEGER PRIMARY KEY REFERENCES incoming_events(event_id),
 verify_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 anidb_id INTEGER NOT NULL REFERENCES identified_anime(anidb_id),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season BETWEEN 0 AND 1000),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode BETWEEN 1 AND 1000),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^a-f0-9]*'),
 source TEXT NOT NULL CHECK(source IN ('magnet','torrent')),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 retired_at_ms INTEGER NOT NULL CHECK(retired_at_ms>=created_at_ms)
);
CREATE INDEX idx_manual_release_archive_episode
 ON manual_release_archive(anidb_id,tvdb_season,tvdb_episode);

INSERT OR ROLLBACK INTO nextgen_v62_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v62_guard;
PRAGMA user_version=62;
COMMIT;
