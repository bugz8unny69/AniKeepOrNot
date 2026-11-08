-- v23 -> v24: verified series/season recognition; never an acquisition authorization.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v24_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v24_guard SELECT user_version=23 FROM pragma_user_version;
CREATE TABLE recognized_seasons (
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>=0),
 anidb_id INTEGER NOT NULL REFERENCES identified_anime(anidb_id),
 display_title TEXT NOT NULL CHECK(length(trim(display_title)) BETWEEN 1 AND 512),
 evidence_verification_id INTEGER NOT NULL REFERENCES release_verifications(verification_id),
 recognized_at_ms INTEGER NOT NULL CHECK(recognized_at_ms>=0),
 PRIMARY KEY(tvdb_series_id,tvdb_season),
 UNIQUE(tvdb_series_id,tvdb_season,anidb_id)
);
CREATE TABLE recognized_aliases (
 alias_key TEXT NOT NULL CHECK(length(alias_key) BETWEEN 3 AND 256),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>=0),
 tvdb_series_id INTEGER NOT NULL,
 anidb_id INTEGER NOT NULL,
 PRIMARY KEY(alias_key,tvdb_season),
 FOREIGN KEY(tvdb_series_id,tvdb_season,anidb_id)
  REFERENCES recognized_seasons(tvdb_series_id,tvdb_season,anidb_id)
);
CREATE INDEX idx_recognized_alias_series ON recognized_aliases(tvdb_series_id,tvdb_season);
DROP TABLE nextgen_v24_guard;
PRAGMA user_version=24;
COMMIT;
