-- v24 -> v25: verified episode placements grouped under recognized seasons.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v25_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v25_guard SELECT user_version=24 FROM pragma_user_version;
CREATE TABLE recognized_episodes (
 tvdb_series_id INTEGER NOT NULL,
 tvdb_season INTEGER NOT NULL,
 anidb_id INTEGER NOT NULL,
 anidb_episode_number INTEGER NOT NULL CHECK(anidb_episode_number>0),
 anidb_episode_id INTEGER NOT NULL CHECK(anidb_episode_id>0),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0),
 tvdb_episode_id INTEGER NOT NULL CHECK(tvdb_episode_id>0),
 evidence_verification_id INTEGER NOT NULL REFERENCES release_verifications(verification_id),
 recognized_at_ms INTEGER NOT NULL CHECK(recognized_at_ms>=0),
 PRIMARY KEY(anidb_id,anidb_episode_number),
 UNIQUE(anidb_episode_id),
 UNIQUE(tvdb_series_id,tvdb_season,tvdb_episode),
 UNIQUE(tvdb_episode_id),
 FOREIGN KEY(tvdb_series_id,tvdb_season,anidb_id)
  REFERENCES recognized_seasons(tvdb_series_id,tvdb_season,anidb_id)
);
CREATE INDEX idx_recognized_episodes_season ON recognized_episodes(tvdb_series_id,tvdb_season);
DROP TABLE nextgen_v25_guard;
PRAGMA user_version=25;
COMMIT;
