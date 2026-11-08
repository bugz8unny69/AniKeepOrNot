-- v26 -> v27: immutable hash-bound verified batch episode facts.
-- No automatic torrent selection, source mutation, Keep or retry.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v27_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v27_guard SELECT user_version=26 FROM pragma_user_version;
CREATE TABLE batch_verifications (
 batch_id INTEGER PRIMARY KEY,
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 identity_result_id INTEGER NOT NULL,
 anidb_id INTEGER NOT NULL CHECK(anidb_id>0),
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>0),
 release_sha256 TEXT NOT NULL CHECK(length(release_sha256)=64 AND release_sha256 NOT GLOB '*[^a-f0-9]*'),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^a-f0-9]*'),
 info_sha256 TEXT NOT NULL CHECK(length(info_sha256)=64 AND info_sha256 NOT GLOB '*[^a-f0-9]*'),
 verified_at_ms INTEGER NOT NULL CHECK(verified_at_ms>=0),
 FOREIGN KEY(identity_result_id,anidb_id) REFERENCES identity_results(result_id,anidb_id)
);
CREATE INDEX idx_batch_verifications_identity ON batch_verifications(identity_result_id);
CREATE TABLE batch_verified_files (
 batch_id INTEGER NOT NULL REFERENCES batch_verifications(batch_id),
 file_index INTEGER NOT NULL CHECK(file_index>=0),
 file_path TEXT NOT NULL CHECK(length(file_path) BETWEEN 1 AND 2048),
 file_size_bytes INTEGER NOT NULL CHECK(file_size_bytes>0),
 release_episode INTEGER NOT NULL CHECK(release_episode>0),
 anidb_episode_number INTEGER NOT NULL CHECK(anidb_episode_number>0),
 anidb_episode_id INTEGER NOT NULL CHECK(anidb_episode_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>0),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0),
 tvdb_episode_id INTEGER NOT NULL CHECK(tvdb_episode_id>0),
 PRIMARY KEY(batch_id,file_index),
 UNIQUE(batch_id,release_episode),
 UNIQUE(batch_id,anidb_episode_id),
 UNIQUE(batch_id,tvdb_season,tvdb_episode),
 UNIQUE(batch_id,tvdb_episode_id)
);
CREATE INDEX idx_batch_files_episode ON batch_verified_files(batch_id,tvdb_season,tvdb_episode);
INSERT OR ROLLBACK INTO nextgen_v27_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v27_guard;
PRAGMA user_version=27;
COMMIT;
