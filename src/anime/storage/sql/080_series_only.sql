-- v79 -> v80: the old per-step pipeline is gone; the series job holds the work (owner, 2026-11-26).
-- Refuses while any old job is unfinished: run fixes/cutover_series_job.py first, which turns
-- unfinished work into series rows and closes those jobs. Then every old job and step table is
-- dropped, with its history. The recognized names and title metadata are rebuilt without their
-- references to the dropped identity and verification tables.
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;
CREATE TEMP TABLE anime_v80_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO anime_v80_guard SELECT user_version=79 FROM pragma_user_version;
INSERT OR ROLLBACK INTO anime_v80_guard
 SELECT count(*)=0 FROM jobs WHERE state NOT IN ('DONE','CANCELED','FAILED');
CREATE TABLE recognized_series_v80 (
 anidb_id INTEGER PRIMARY KEY,
 display_title TEXT NOT NULL CHECK(length(trim(display_title)) BETWEEN 1 AND 512),
 tvdb_series_id INTEGER CHECK(tvdb_series_id IS NULL OR tvdb_series_id>0),
 anime_type TEXT CHECK(anime_type IS NULL OR length(trim(anime_type)) BETWEEN 1 AND 64),
 recognized_at_ms INTEGER NOT NULL CHECK(recognized_at_ms>=0)
);
INSERT INTO recognized_series_v80(anidb_id,display_title,tvdb_series_id,anime_type,recognized_at_ms) SELECT anidb_id,display_title,tvdb_series_id,anime_type,recognized_at_ms FROM recognized_series;
DROP TABLE recognized_series;
ALTER TABLE recognized_series_v80 RENAME TO recognized_series;
CREATE TABLE anime_title_metadata_v80 (
 anidb_id INTEGER PRIMARY KEY,
 english_title TEXT CHECK(english_title IS NULL OR length(trim(english_title)) BETWEEN 1 AND 512),
 romaji_title TEXT CHECK(romaji_title IS NULL OR length(trim(romaji_title)) BETWEEN 1 AND 512),
 CHECK(english_title IS NOT NULL OR romaji_title IS NOT NULL)
);
INSERT INTO anime_title_metadata_v80(anidb_id,english_title,romaji_title) SELECT anidb_id,english_title,romaji_title FROM anime_title_metadata;
DROP TABLE anime_title_metadata;
ALTER TABLE anime_title_metadata_v80 RENAME TO anime_title_metadata;
CREATE TABLE recognized_seasons_v80 (
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>=0),
 anidb_id INTEGER NOT NULL,
 display_title TEXT NOT NULL CHECK(length(trim(display_title)) BETWEEN 1 AND 512),
 recognized_at_ms INTEGER NOT NULL CHECK(recognized_at_ms>=0),
 PRIMARY KEY(tvdb_series_id,tvdb_season,anidb_id)
);
INSERT INTO recognized_seasons_v80(tvdb_series_id,tvdb_season,anidb_id,display_title,recognized_at_ms) SELECT tvdb_series_id,tvdb_season,anidb_id,display_title,recognized_at_ms FROM recognized_seasons;
DROP TABLE recognized_seasons;
ALTER TABLE recognized_seasons_v80 RENAME TO recognized_seasons;
CREATE TABLE recognized_episodes_v80 (
 tvdb_series_id INTEGER CHECK(tvdb_series_id IS NULL OR tvdb_series_id>0),
 tvdb_season INTEGER CHECK(tvdb_season IS NULL OR tvdb_season>=0),
 anidb_id INTEGER NOT NULL REFERENCES recognized_series(anidb_id),
 anidb_episode_number INTEGER NOT NULL CHECK(anidb_episode_number>0),
 anidb_episode_id INTEGER NOT NULL CHECK(anidb_episode_id>0),
 tvdb_episode INTEGER CHECK(tvdb_episode IS NULL OR tvdb_episode>0),
 tvdb_episode_id INTEGER CHECK(tvdb_episode_id IS NULL OR tvdb_episode_id>0),
 recognized_at_ms INTEGER NOT NULL CHECK(recognized_at_ms>=0),
 PRIMARY KEY(anidb_id,anidb_episode_number),
 UNIQUE(anidb_episode_id),
 UNIQUE(tvdb_series_id,tvdb_season,tvdb_episode),
 UNIQUE(tvdb_episode_id),
 CHECK((tvdb_series_id IS NULL AND tvdb_season IS NULL AND tvdb_episode IS NULL AND tvdb_episode_id IS NULL) OR
       (tvdb_series_id IS NOT NULL AND tvdb_season IS NOT NULL AND tvdb_episode IS NOT NULL AND tvdb_episode_id IS NOT NULL))
);
INSERT INTO recognized_episodes_v80(tvdb_series_id,tvdb_season,anidb_id,anidb_episode_number,anidb_episode_id,tvdb_episode,tvdb_episode_id,recognized_at_ms) SELECT tvdb_series_id,tvdb_season,anidb_id,anidb_episode_number,anidb_episode_id,tvdb_episode,tvdb_episode_id,recognized_at_ms FROM recognized_episodes;
DROP TABLE recognized_episodes;
ALTER TABLE recognized_episodes_v80 RENAME TO recognized_episodes;
CREATE INDEX idx_recognized_episodes_season ON recognized_episodes(tvdb_series_id,tvdb_season);
DROP TABLE acquisition_budgets;
DROP TABLE anime_review_decisions;
DROP TABLE batch_cancellation_cleanup;
DROP TABLE batch_candidate_events;
DROP TABLE batch_candidate_stops;
DROP TABLE batch_download_cleanup;
DROP TABLE batch_episode_reservations;
DROP TABLE batch_keep_cleanup;
DROP TABLE batch_keep_imports;
DROP TABLE batch_metadata_admissions;
DROP TABLE batch_probe_stops;
DROP TABLE batch_submission_inventories;
DROP TABLE batch_verified_files;
DROP TABLE cancellation_cleanup;
DROP TABLE episode_discovery_results;
DROP TABLE episode_release_candidates;
DROP TABLE episode_release_selections;
DROP TABLE event_history;
DROP TABLE identity_candidate_evidence;
DROP TABLE job_history;
DROP TABLE job_operation_members;
DROP TABLE keep_imports;
DROP TABLE magnet_intents;
DROP TABLE management_actions;
DROP TABLE manual_release_archive;
DROP TABLE manual_release_submissions;
DROP TABLE media_probe_details;
DROP TABLE prowlarr_search_snapshots;
DROP TABLE resource_operations;
DROP TABLE result_episode_links;
DROP TABLE staging_cleanup_artifacts;
DROP TABLE staging_publications;
DROP TABLE torrent_transfers;
DROP TABLE transcode_batches;
DROP TABLE transcode_media;
DROP TABLE user_evaluation_checks;
DROP TABLE batch_candidates;
DROP TABLE batch_staging_publications;
DROP TABLE media_verifications;
DROP TABLE resources;
DROP TABLE torrent_submissions;
DROP TABLE acquisition_plans;
DROP TABLE batch_candidate_scopes;
DROP TABLE batch_episode_media;
DROP TABLE batch_torrent_unlocks;
DROP TABLE batch_user_evaluation_files;
DROP TABLE episode_reservations;
DROP TABLE job_operations;
DROP TABLE batch_probe_media;
DROP TABLE user_evaluation_plans;
DROP TABLE batch_torrent_transfers;
DROP TABLE release_verifications;
DROP TABLE batch_torrent_submissions;
DROP TABLE identified_episodes;
DROP TABLE batch_user_evaluation_plans;
DROP TABLE batch_verifications;
DROP TABLE identity_results;
DROP TABLE jobs;
DROP TABLE identified_anime;
DROP TABLE incoming_events;
DROP TABLE anime_v80_guard;
PRAGMA user_version=80;
COMMIT;
