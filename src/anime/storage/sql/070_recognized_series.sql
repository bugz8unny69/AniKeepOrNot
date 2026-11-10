-- v69 -> v70: Recognized names track series (AniDB anime) and their episodes,
-- not TVDB seasons (work item P2). recognized_series has one row per AniDB
-- anime; TVDB series is an optional label. Aliases map to the AniDB anime.
-- Recognized episodes may have no TVDB placement. recognized_seasons stays as
-- the season label of anime whose placement is known.
-- Every existing season, alias and episode is kept. Every identified anime
-- without a series row gets one with its title. An anime recognized under two
-- TVDB series, or under another TVDB series than its identity, stops the
-- migration (nothing is merged silently).
BEGIN IMMEDIATE;
CREATE TEMP TABLE anime_v70_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO anime_v70_guard SELECT user_version=69 FROM pragma_user_version;
CREATE TEMP TABLE anime_v70_conflicts(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO anime_v70_conflicts
SELECT count(*)=0 FROM recognized_seasons s JOIN identified_anime a USING(anidb_id)
 WHERE s.tvdb_series_id<>a.tvdb_series_id;

CREATE TABLE recognized_series (
 anidb_id INTEGER PRIMARY KEY REFERENCES identified_anime(anidb_id),
 display_title TEXT NOT NULL CHECK(length(trim(display_title)) BETWEEN 1 AND 512),
 tvdb_series_id INTEGER CHECK(tvdb_series_id IS NULL OR tvdb_series_id>0),
 anime_type TEXT CHECK(anime_type IS NULL OR length(trim(anime_type)) BETWEEN 1 AND 64),
 recognized_at_ms INTEGER NOT NULL CHECK(recognized_at_ms>=0)
);
INSERT INTO recognized_series(anidb_id,display_title,tvdb_series_id,anime_type,recognized_at_ms)
SELECT a.anidb_id,
 COALESCE((SELECT s.display_title FROM recognized_seasons s WHERE s.anidb_id=a.anidb_id
           ORDER BY s.recognized_at_ms,s.tvdb_season LIMIT 1),substr(trim(a.title),1,512)),
 a.tvdb_series_id,NULL,
 COALESCE((SELECT min(s.recognized_at_ms) FROM recognized_seasons s WHERE s.anidb_id=a.anidb_id),
          (SELECT min(r.validated_at_ms) FROM identity_results r WHERE r.anidb_id=a.anidb_id),0)
FROM identified_anime a;

CREATE TABLE recognized_aliases_v70 (
 alias_key TEXT NOT NULL CHECK(length(alias_key) BETWEEN 3 AND 256),
 anidb_id INTEGER NOT NULL REFERENCES recognized_series(anidb_id),
 PRIMARY KEY(alias_key,anidb_id)
);
INSERT OR IGNORE INTO recognized_aliases_v70(alias_key,anidb_id) SELECT alias_key,anidb_id FROM recognized_aliases;
DROP TABLE recognized_aliases;
ALTER TABLE recognized_aliases_v70 RENAME TO recognized_aliases;
CREATE INDEX idx_recognized_alias_series ON recognized_aliases(anidb_id);

CREATE TABLE recognized_episodes_v70 (
 tvdb_series_id INTEGER CHECK(tvdb_series_id IS NULL OR tvdb_series_id>0),
 tvdb_season INTEGER CHECK(tvdb_season IS NULL OR tvdb_season>=0),
 anidb_id INTEGER NOT NULL REFERENCES recognized_series(anidb_id),
 anidb_episode_number INTEGER NOT NULL CHECK(anidb_episode_number>0),
 anidb_episode_id INTEGER NOT NULL CHECK(anidb_episode_id>0),
 tvdb_episode INTEGER CHECK(tvdb_episode IS NULL OR tvdb_episode>0),
 tvdb_episode_id INTEGER CHECK(tvdb_episode_id IS NULL OR tvdb_episode_id>0),
 evidence_verification_id INTEGER NOT NULL REFERENCES release_verifications(verification_id),
 recognized_at_ms INTEGER NOT NULL CHECK(recognized_at_ms>=0),
 PRIMARY KEY(anidb_id,anidb_episode_number),
 UNIQUE(anidb_episode_id),
 UNIQUE(tvdb_series_id,tvdb_season,tvdb_episode),
 UNIQUE(tvdb_episode_id),
 CHECK((tvdb_series_id IS NULL AND tvdb_season IS NULL AND tvdb_episode IS NULL AND tvdb_episode_id IS NULL) OR
       (tvdb_series_id IS NOT NULL AND tvdb_season IS NOT NULL AND tvdb_episode IS NOT NULL AND tvdb_episode_id IS NOT NULL))
);
INSERT INTO recognized_episodes_v70(tvdb_series_id,tvdb_season,anidb_id,anidb_episode_number,anidb_episode_id,
 tvdb_episode,tvdb_episode_id,evidence_verification_id,recognized_at_ms)
SELECT tvdb_series_id,tvdb_season,anidb_id,anidb_episode_number,anidb_episode_id,
 tvdb_episode,tvdb_episode_id,evidence_verification_id,recognized_at_ms FROM recognized_episodes;
DROP TABLE recognized_episodes;
ALTER TABLE recognized_episodes_v70 RENAME TO recognized_episodes;
CREATE INDEX idx_recognized_episodes_season ON recognized_episodes(tvdb_series_id,tvdb_season);

DROP TABLE anime_v70_conflicts;
DROP TABLE anime_v70_guard;
PRAGMA user_version=70;
COMMIT;
