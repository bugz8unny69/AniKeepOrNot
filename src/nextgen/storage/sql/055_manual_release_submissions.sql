-- v54 -> v55: independent manual candidates, without a second active
-- episode reservation. Original BEP3 bytes remain local and bounded.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v55_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v55_guard SELECT user_version=54 FROM pragma_user_version;
CREATE TABLE manual_release_submissions (
 event_id INTEGER PRIMARY KEY REFERENCES incoming_events(event_id),
 verify_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 anidb_id INTEGER NOT NULL REFERENCES identified_anime(anidb_id),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>=0 AND tvdb_season<=1000),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode BETWEEN 1 AND 1000),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^a-f0-9]*'),
 source TEXT NOT NULL CHECK(source IN ('magnet','torrent')),
 metainfo BLOB,
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 UNIQUE(anidb_id,tvdb_season,tvdb_episode,info_hash),
 CHECK((source='magnet' AND metainfo IS NULL) OR
       (source='torrent' AND length(metainfo) BETWEEN 1 AND 4194304))
);
CREATE INDEX idx_manual_release_episode ON manual_release_submissions(anidb_id,tvdb_season,tvdb_episode);
INSERT OR ROLLBACK INTO nextgen_v55_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v55_guard;
PRAGMA user_version=55;
COMMIT;
