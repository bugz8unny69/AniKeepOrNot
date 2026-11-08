-- v48 -> v49: operator-confirmed series identity, scoped to a release alias and TVDB season.
-- This is NOT a verified episode placement and never seeds recognized_episodes.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v49_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v49_guard SELECT user_version=48 FROM pragma_user_version;
CREATE TABLE manual_identity_matches (
 alias_key TEXT NOT NULL CHECK(length(alias_key) BETWEEN 3 AND 256),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season BETWEEN -1 AND 999),
 anidb_id INTEGER NOT NULL CHECK(anidb_id>0),
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 512),
 PRIMARY KEY(alias_key,tvdb_season)
);
DROP TABLE nextgen_v49_guard;
PRAGMA user_version=49;
COMMIT;
