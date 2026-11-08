-- v9 -> v10: declared-episode identity evidence; no acquisition authorization.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v10_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v10_guard SELECT user_version=9 FROM pragma_user_version;
CREATE TABLE release_verifications (
 verification_id INTEGER PRIMARY KEY,
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 identity_result_id INTEGER NOT NULL,
 anidb_id INTEGER NOT NULL,
 release_sha256 TEXT NOT NULL CHECK(length(release_sha256)=64 AND release_sha256 NOT GLOB '*[^a-f0-9]*'),
 verification_scope TEXT NOT NULL CHECK(verification_scope='DECLARED_SINGLE_EPISODE'),
 anidb_episode_number INTEGER NOT NULL CHECK(anidb_episode_number>0),
 anidb_episode_id INTEGER NOT NULL,
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>=0),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0),
 tvdb_episode_id INTEGER NOT NULL CHECK(tvdb_episode_id>0),
 tvdb_order TEXT NOT NULL DEFAULT 'official' CHECK(tvdb_order='official'),
 verified_at_ms INTEGER NOT NULL CHECK(verified_at_ms>=0),
 FOREIGN KEY(identity_result_id,anidb_id) REFERENCES identity_results(result_id,anidb_id),
 FOREIGN KEY(anidb_episode_id,anidb_id) REFERENCES identified_episodes(anidb_episode_id,anidb_id)
);
CREATE INDEX idx_release_identity ON release_verifications(identity_result_id);
DROP TABLE nextgen_v10_guard;
PRAGMA user_version=10;
COMMIT;
