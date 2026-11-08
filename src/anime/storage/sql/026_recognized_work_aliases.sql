-- v25 -> v26: preserve all verified works and ambiguous aliases.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v26_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v26_guard SELECT user_version=25 FROM pragma_user_version;
CREATE TEMP TABLE saved_recognized_seasons AS SELECT * FROM recognized_seasons;
CREATE TEMP TABLE saved_recognized_aliases AS SELECT * FROM recognized_aliases;
CREATE TEMP TABLE saved_recognized_episodes AS SELECT * FROM recognized_episodes;
DROP TABLE recognized_episodes;
DROP TABLE recognized_aliases;
DROP TABLE recognized_seasons;
CREATE TABLE recognized_seasons (
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>=0),
 anidb_id INTEGER NOT NULL REFERENCES identified_anime(anidb_id),
 display_title TEXT NOT NULL CHECK(length(trim(display_title)) BETWEEN 1 AND 512),
 evidence_verification_id INTEGER NOT NULL REFERENCES release_verifications(verification_id),
 recognized_at_ms INTEGER NOT NULL CHECK(recognized_at_ms>=0),
 PRIMARY KEY(tvdb_series_id,tvdb_season,anidb_id)
);
CREATE TABLE recognized_aliases (
 alias_key TEXT NOT NULL CHECK(length(alias_key) BETWEEN 3 AND 256),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>=0),
 tvdb_series_id INTEGER NOT NULL,
 anidb_id INTEGER NOT NULL,
 PRIMARY KEY(alias_key,tvdb_season,tvdb_series_id,anidb_id),
 FOREIGN KEY(tvdb_series_id,tvdb_season,anidb_id)
  REFERENCES recognized_seasons(tvdb_series_id,tvdb_season,anidb_id)
);
CREATE INDEX idx_recognized_alias_series ON recognized_aliases(tvdb_series_id,tvdb_season);

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
INSERT INTO recognized_seasons SELECT * FROM saved_recognized_seasons;
DROP TABLE saved_recognized_seasons;
INSERT INTO recognized_aliases SELECT * FROM saved_recognized_aliases;
DROP TABLE saved_recognized_aliases;
INSERT INTO recognized_episodes SELECT * FROM saved_recognized_episodes;
DROP TABLE saved_recognized_episodes;
DROP TABLE nextgen_v26_guard;
PRAGMA user_version=26;
COMMIT;
