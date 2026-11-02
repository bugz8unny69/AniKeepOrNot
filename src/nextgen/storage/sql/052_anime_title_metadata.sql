-- v51 -> v52: optional display translations, independent of verified identity.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v52_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v52_guard SELECT user_version=51 FROM pragma_user_version;
CREATE TABLE anime_title_metadata (
 anidb_id INTEGER PRIMARY KEY REFERENCES identified_anime(anidb_id),
 english_title TEXT CHECK(english_title IS NULL OR length(trim(english_title)) BETWEEN 1 AND 512),
 romaji_title TEXT CHECK(romaji_title IS NULL OR length(trim(romaji_title)) BETWEEN 1 AND 512),
 CHECK(english_title IS NOT NULL OR romaji_title IS NOT NULL)
);
DROP TABLE nextgen_v52_guard;
PRAGMA user_version=52;
COMMIT;
