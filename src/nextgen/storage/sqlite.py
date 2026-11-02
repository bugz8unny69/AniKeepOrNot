"""NextGen-only SQLite persistence. No workers, web endpoints, or legacy migrations."""
from contextlib import contextmanager
import json
import logging
import sqlite3
import time

from src.nextgen.history import append_event_history, append_job_history, component_name, integer
from src.nextgen.diagnostics import log_history

SCHEMA = """
CREATE TABLE IF NOT EXISTS incoming_events (
 event_id INTEGER PRIMARY KEY,
 source TEXT NOT NULL,
 raw_body BLOB NOT NULL,
 received_at_ms INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'PENDING'
  CHECK(state IN ('PENDING','RUNNING','RETRY_WAIT','IDENTIFIED','NEEDS_REVIEW')),
 attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
 next_run_at_ms INTEGER,
 lease_token TEXT,
 lease_expires_at_ms INTEGER,
 last_reason_code TEXT
);
CREATE TABLE IF NOT EXISTS event_history (
 history_id INTEGER PRIMARY KEY,
 event_id INTEGER NOT NULL REFERENCES incoming_events(event_id),
 occurred_at_ms INTEGER NOT NULL,
 event_code TEXT NOT NULL,
 reason_code TEXT,
 details_json TEXT,
 component TEXT CHECK(component IS NULL OR component IN
  ('supervisor','intake','coordinator','worker','identity','acquisition','api'))
);
CREATE TABLE IF NOT EXISTS manual_identity_matches (
 alias_key TEXT NOT NULL CHECK(length(alias_key) BETWEEN 3 AND 256),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season BETWEEN -1 AND 999),
 anidb_id INTEGER NOT NULL CHECK(anidb_id>0),
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 512),
 PRIMARY KEY(alias_key,tvdb_season)
);
CREATE TABLE IF NOT EXISTS identified_anime (
 anidb_id INTEGER PRIMARY KEY CHECK(anidb_id > 0),
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id > 0),
 title TEXT NOT NULL CHECK(length(trim(title)) > 0)
);
CREATE TABLE IF NOT EXISTS anime_title_metadata (
 anidb_id INTEGER PRIMARY KEY REFERENCES identified_anime(anidb_id),
 english_title TEXT CHECK(english_title IS NULL OR length(trim(english_title)) BETWEEN 1 AND 512),
 romaji_title TEXT CHECK(romaji_title IS NULL OR length(trim(romaji_title)) BETWEEN 1 AND 512),
 CHECK(english_title IS NOT NULL OR romaji_title IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS identified_episodes (
 anidb_episode_id INTEGER PRIMARY KEY CHECK(anidb_episode_id > 0),
 anidb_id INTEGER NOT NULL REFERENCES identified_anime(anidb_id),
 UNIQUE(anidb_episode_id, anidb_id)
);
CREATE TABLE IF NOT EXISTS identity_results (
 result_id INTEGER PRIMARY KEY,
 event_id INTEGER NOT NULL REFERENCES incoming_events(event_id),
 anidb_id INTEGER NOT NULL REFERENCES identified_anime(anidb_id),
 schema_version INTEGER NOT NULL CHECK(schema_version=2),
 series_json TEXT NOT NULL CHECK(json_valid(series_json)),
 validated_at_ms INTEGER NOT NULL,
 UNIQUE(event_id,anidb_id),
 UNIQUE(result_id,anidb_id)
);
CREATE TABLE IF NOT EXISTS result_episode_links (
 result_id INTEGER NOT NULL,
 anidb_id INTEGER NOT NULL,
 anidb_episode_id INTEGER NOT NULL,
 PRIMARY KEY(result_id,anidb_episode_id),
 FOREIGN KEY(result_id,anidb_id) REFERENCES identity_results(result_id,anidb_id),
 FOREIGN KEY(anidb_episode_id,anidb_id)
  REFERENCES identified_episodes(anidb_episode_id,anidb_id)
);

-- Jobs are independent units of work, not a parent/child dependency graph.
-- created_by_job_id is optional provenance only.
CREATE TABLE IF NOT EXISTS jobs (
 job_id INTEGER PRIMARY KEY,
 event_id INTEGER REFERENCES incoming_events(event_id),
 created_by_job_id INTEGER REFERENCES jobs(job_id),
 kind TEXT NOT NULL CHECK(length(trim(kind)) > 0),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 state TEXT NOT NULL DEFAULT 'PENDING'
   CHECK(state IN ('PENDING','RUNNING','RETRY_WAIT','ON_HOLD','NEEDS_REVIEW','DONE','BLOCKED','CANCELED')),
 available_at_ms INTEGER NOT NULL,
 attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
 lease_token TEXT,
 lease_expires_at_ms INTEGER,
 last_reason_code TEXT,
 created_at_ms INTEGER NOT NULL,
 updated_at_ms INTEGER NOT NULL,
 activity_code TEXT CHECK(activity_code IS NULL OR activity_code IN
  ('DOWNLOADING','WAITING_FOR_DOWNLOAD','WAITING_FOR_SEARCH','SKIPPED_DUPLICATE')),
 CHECK(activity_code IS NULL OR
       (activity_code='DOWNLOADING' AND state IN ('RUNNING','RETRY_WAIT')) OR
       (activity_code IN ('WAITING_FOR_DOWNLOAD','WAITING_FOR_SEARCH') AND state='RETRY_WAIT') OR
       (activity_code='SKIPPED_DUPLICATE' AND state='DONE')),
 CHECK(created_by_job_id IS NULL OR created_by_job_id <> job_id),
 CHECK(state <> 'RUNNING' OR
       (lease_token IS NOT NULL AND lease_expires_at_ms IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(state,available_at_ms,job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_expired ON jobs(state,lease_expires_at_ms);
CREATE INDEX IF NOT EXISTS idx_jobs_event ON jobs(event_id);
-- Job history is append-only operational audit, separate from incoming event_history.
CREATE TABLE IF NOT EXISTS job_history (
 history_id INTEGER PRIMARY KEY,
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 occurred_at_ms INTEGER NOT NULL,
 event_code TEXT NOT NULL CHECK(length(trim(event_code)) > 0),
 reason_code TEXT,
 details_json TEXT CHECK(details_json IS NULL OR json_valid(details_json)),
 component TEXT CHECK(component IS NULL OR component IN
  ('supervisor','intake','coordinator','worker','identity','acquisition','api')),
 activity_code TEXT CHECK(activity_code IS NULL OR activity_code IN
  ('DOWNLOADING','WAITING_FOR_DOWNLOAD','WAITING_FOR_SEARCH','SKIPPED_DUPLICATE')),
 handler_code TEXT,
 handler_label TEXT,
 invocation_id TEXT,
 CHECK((handler_code IS NULL AND handler_label IS NULL AND invocation_id IS NULL) OR
       (handler_code IS NOT NULL AND handler_label IS NOT NULL AND invocation_id IS NOT NULL
        AND length(handler_code) BETWEEN 1 AND 64 AND handler_code GLOB '[A-Z]*'
        AND handler_code NOT GLOB '*[^A-Z0-9_]*'
        AND length(handler_label) BETWEEN 1 AND 128 AND length(trim(handler_label)) > 0
        AND length(invocation_id)=32 AND invocation_id NOT GLOB '*[^a-f0-9]*'))
);
CREATE INDEX IF NOT EXISTS idx_job_history_invocation ON job_history(invocation_id,history_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_handler_invocation_start ON job_history(invocation_id)
 WHERE event_code='HANDLER_STARTED';
CREATE UNIQUE INDEX IF NOT EXISTS idx_handler_invocation_end ON job_history(invocation_id)
 WHERE event_code IN ('HANDLER_COMPLETED','HANDLER_FAILED');
CREATE INDEX IF NOT EXISTS idx_job_history_job ON job_history(job_id,history_id);
CREATE INDEX IF NOT EXISTS idx_event_queue ON incoming_events(state,next_run_at_ms);
CREATE INDEX IF NOT EXISTS idx_event_lease ON incoming_events(state,lease_expires_at_ms);
CREATE INDEX IF NOT EXISTS idx_history_event ON event_history(event_id,history_id);
CREATE INDEX IF NOT EXISTS idx_anime_tvdb ON identified_anime(tvdb_series_id);
CREATE INDEX IF NOT EXISTS idx_episodes_anime ON identified_episodes(anidb_id);
CREATE INDEX IF NOT EXISTS idx_links_episode ON result_episode_links(anidb_episode_id);
CREATE TABLE IF NOT EXISTS provider_limits (
 provider TEXT PRIMARY KEY CHECK(provider IN ('anidb','anidb_titles','anime_lists','tvdb')),
 next_allowed_at_ms INTEGER NOT NULL CHECK(next_allowed_at_ms >= 0),
 failures INTEGER NOT NULL DEFAULT 0 CHECK(failures BETWEEN 0 AND 16),
 reservation_token TEXT NOT NULL CHECK(length(reservation_token)=32)
);
CREATE TABLE IF NOT EXISTS provider_public_cache (
 provider TEXT NOT NULL CHECK(provider IN ('anidb','anidb_titles','anime_lists')),
 cache_key TEXT NOT NULL CHECK(length(cache_key) BETWEEN 1 AND 64),
 fetched_at_ms INTEGER NOT NULL CHECK(fetched_at_ms >= 0),
 expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms > fetched_at_ms),
 payload BLOB NOT NULL CHECK(typeof(payload)='blob' AND length(payload) BETWEEN 1 AND 67108864),
 PRIMARY KEY(provider,cache_key)
);
CREATE INDEX IF NOT EXISTS idx_provider_cache_expiry ON provider_public_cache(expires_at_ms);

CREATE TABLE IF NOT EXISTS release_verifications (
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
CREATE INDEX IF NOT EXISTS idx_release_identity ON release_verifications(identity_result_id);

CREATE TABLE IF NOT EXISTS recognized_seasons (
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>=0),
 anidb_id INTEGER NOT NULL REFERENCES identified_anime(anidb_id),
 display_title TEXT NOT NULL CHECK(length(trim(display_title)) BETWEEN 1 AND 512),
 evidence_verification_id INTEGER NOT NULL REFERENCES release_verifications(verification_id),
 recognized_at_ms INTEGER NOT NULL CHECK(recognized_at_ms>=0),
 PRIMARY KEY(tvdb_series_id,tvdb_season,anidb_id)
);
CREATE TABLE IF NOT EXISTS recognized_aliases (
 alias_key TEXT NOT NULL CHECK(length(alias_key) BETWEEN 3 AND 256),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>=0),
 tvdb_series_id INTEGER NOT NULL,
 anidb_id INTEGER NOT NULL,
 PRIMARY KEY(alias_key,tvdb_season,tvdb_series_id,anidb_id),
 FOREIGN KEY(tvdb_series_id,tvdb_season,anidb_id)
  REFERENCES recognized_seasons(tvdb_series_id,tvdb_season,anidb_id)
);
CREATE INDEX IF NOT EXISTS idx_recognized_alias_series ON recognized_aliases(tvdb_series_id,tvdb_season);

CREATE TABLE IF NOT EXISTS recognized_episodes (
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
CREATE INDEX IF NOT EXISTS idx_recognized_episodes_season ON recognized_episodes(tvdb_series_id,tvdb_season);

CREATE TABLE IF NOT EXISTS trial_plans (
 plan_id INTEGER PRIMARY KEY,
 anidb_id INTEGER NOT NULL REFERENCES identified_anime(anidb_id),
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>=0),
 tvdb_order TEXT NOT NULL DEFAULT 'official' CHECK(tvdb_order='official'),
 anchor_verification_id INTEGER NOT NULL REFERENCES release_verifications(verification_id),
 anchor_episode INTEGER NOT NULL CHECK(anchor_episode>0),
 backfill_count INTEGER NOT NULL CHECK(backfill_count BETWEEN 0 AND 1000),
 following_count INTEGER NOT NULL CHECK(following_count BETWEEN 0 AND 1000),
 configuration_revision INTEGER NOT NULL CHECK(configuration_revision>0),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 UNIQUE(anidb_id,tvdb_series_id,tvdb_season,tvdb_order),
 CHECK(backfill_count>0 OR following_count>0),
 CHECK(following_count=0 OR anchor_episode<=9223372036854775807-following_count+1)
);
CREATE TABLE IF NOT EXISTS trial_evaluations (
 evaluation_id INTEGER PRIMARY KEY,
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 verification_id INTEGER NOT NULL REFERENCES release_verifications(verification_id),
 plan_id INTEGER REFERENCES trial_plans(plan_id),
 selected INTEGER NOT NULL CHECK(selected IN (0,1)),
 reason_code TEXT NOT NULL CHECK(reason_code IN ('TRIAL_SELECTED','TRIAL_OUTSIDE_WINDOW','TRIAL_DISABLED')),
 evaluated_at_ms INTEGER NOT NULL CHECK(evaluated_at_ms>=0),
 CHECK((plan_id IS NULL AND selected=0 AND reason_code='TRIAL_DISABLED') OR
       (plan_id IS NOT NULL AND ((selected=1 AND reason_code='TRIAL_SELECTED') OR
                                (selected=0 AND reason_code='TRIAL_OUTSIDE_WINDOW'))))
);
CREATE INDEX IF NOT EXISTS idx_trial_evaluations_plan ON trial_evaluations(plan_id);

CREATE TABLE IF NOT EXISTS episode_reservations (
 reservation_id INTEGER PRIMARY KEY,
 owner_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 evaluation_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 verification_id INTEGER NOT NULL REFERENCES release_verifications(verification_id),
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 anidb_id INTEGER NOT NULL REFERENCES identified_anime(anidb_id),
 anidb_episode_id INTEGER NOT NULL REFERENCES identified_episodes(anidb_episode_id),
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>=0),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0),
 tvdb_episode_id INTEGER NOT NULL CHECK(tvdb_episode_id>0),
 tvdb_order TEXT NOT NULL DEFAULT 'official' CHECK(tvdb_order='official'),
 reserved_at_ms INTEGER NOT NULL CHECK(reserved_at_ms>=0),
 state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(state IN ('ACTIVE','RELEASED')),
 ended_at_ms INTEGER CHECK(ended_at_ms IS NULL OR ended_at_ms>=0),
 ended_by_job_id INTEGER REFERENCES jobs(job_id),
 CHECK((state='ACTIVE' AND ended_at_ms IS NULL AND ended_by_job_id IS NULL) OR
       (state='RELEASED' AND ended_at_ms IS NOT NULL AND ended_by_job_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_episode_active_anidb ON episode_reservations(anidb_episode_id) WHERE state='ACTIVE';
CREATE UNIQUE INDEX IF NOT EXISTS idx_episode_active_tvdb ON episode_reservations(tvdb_episode_id) WHERE state='ACTIVE';
CREATE UNIQUE INDEX IF NOT EXISTS idx_episode_active_placement ON episode_reservations(tvdb_series_id,tvdb_season,tvdb_episode,tvdb_order) WHERE state='ACTIVE';

CREATE TABLE IF NOT EXISTS acquisition_plans (
 plan_id INTEGER PRIMARY KEY,
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 reservation_id INTEGER NOT NULL UNIQUE REFERENCES episode_reservations(reservation_id),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^a-f0-9]*'),
 info_sha256 TEXT CHECK(length(info_sha256)=64 AND info_sha256 NOT GLOB '*[^a-f0-9]*'),
 manifest_json TEXT NOT NULL CHECK(length(CAST(manifest_json AS BLOB)) BETWEEN 1 AND 1048576),
 submission_marker TEXT NOT NULL UNIQUE CHECK(length(submission_marker)=32 AND submission_marker NOT GLOB '*[^a-f0-9]*'),
 state TEXT NOT NULL DEFAULT 'PREPARED' CHECK(state IN ('PREPARED','RELEASED')),
 configuration_revision INTEGER NOT NULL CHECK(configuration_revision>0),
 prepared_at_ms INTEGER NOT NULL CHECK(prepared_at_ms>=0),
 selected_size_bytes INTEGER CHECK(selected_size_bytes IS NULL OR selected_size_bytes>0)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_acquisition_active_hash ON acquisition_plans(info_hash) WHERE state='PREPARED';

CREATE TABLE IF NOT EXISTS acquisition_budgets (
 trial_plan_id INTEGER PRIMARY KEY REFERENCES trial_plans(plan_id),
 max_episode_bytes INTEGER NOT NULL CHECK(max_episode_bytes BETWEEN 1000000 AND 1000000000000 AND max_episode_bytes%1000000=0),
 max_plan_bytes INTEGER NOT NULL CHECK(max_plan_bytes BETWEEN 1000000 AND 1000000000000 AND max_plan_bytes%1000000=0),
 configuration_revision INTEGER NOT NULL CHECK(configuration_revision>0),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0)
);

CREATE TABLE IF NOT EXISTS torrent_submissions (
 plan_id INTEGER PRIMARY KEY REFERENCES acquisition_plans(plan_id),
 first_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 first_attempt INTEGER NOT NULL CHECK(first_attempt>0),
 base_url TEXT NOT NULL CHECK(length(base_url) BETWEEN 1 AND 2048),
 save_path TEXT NOT NULL CHECK(length(save_path) BETWEEN 1 AND 4096),
 category TEXT NOT NULL CHECK(length(category) BETWEEN 1 AND 64),
 timeout_seconds INTEGER NOT NULL CHECK(timeout_seconds BETWEEN 1 AND 60),
 api_major INTEGER NOT NULL CHECK(api_major IN (4,5)),
 fingerprint TEXT NOT NULL UNIQUE CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^a-f0-9]*'),
 configuration_revision INTEGER NOT NULL CHECK(configuration_revision>0),
 state TEXT NOT NULL CHECK(state IN ('UNCERTAIN','EXTERNAL','REVIEW','OWNED_STOPPED')),
 reason_code TEXT NOT NULL,
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms),
 CHECK((state='UNCERTAIN' AND reason_code='TORRENT_SUBMISSION_UNCERTAIN') OR
       (state='EXTERNAL' AND reason_code='TORRENT_ALREADY_PRESENT') OR
       (state='REVIEW' AND reason_code='TORRENT_PROVENANCE_CONFLICT') OR
       (state='OWNED_STOPPED' AND reason_code='TORRENT_STOPPED_CONFIRMED'))
);

CREATE TABLE IF NOT EXISTS torrent_transfers (
 plan_id INTEGER PRIMARY KEY REFERENCES torrent_submissions(plan_id),
 selection_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 selection_attempt INTEGER NOT NULL CHECK(selection_attempt>0),
 start_job_id INTEGER REFERENCES jobs(job_id),
 start_attempt INTEGER CHECK(start_attempt IS NULL OR start_attempt>0),
 selected_index INTEGER NOT NULL CHECK(selected_index>=0),
 piece_size INTEGER NOT NULL CHECK(piece_size BETWEEN 1 AND 67108864),
 overlap_bytes INTEGER NOT NULL DEFAULT 0 CHECK(overlap_bytes=0),
 fingerprint TEXT NOT NULL UNIQUE CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^a-f0-9]*'),
 phase TEXT NOT NULL CHECK(phase IN ('SELECT_UNCERTAIN','SELECTED','START_UNCERTAIN','ACTIVE','COMPLETE','REVIEW')),
 reason_code TEXT NOT NULL,
 selected_progress REAL NOT NULL DEFAULT 0 CHECK(selected_progress BETWEEN 0 AND 1),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms),
 CHECK((start_job_id IS NULL AND start_attempt IS NULL) OR (start_job_id IS NOT NULL AND start_attempt IS NOT NULL)),
 CHECK(phase NOT IN ('START_UNCERTAIN','ACTIVE') OR start_job_id IS NOT NULL),
 CHECK((phase='SELECT_UNCERTAIN' AND reason_code='TORRENT_SELECTION_UNCERTAIN') OR
       (phase='SELECTED' AND reason_code='TORRENT_SELECTION_CONFIRMED') OR
       (phase='START_UNCERTAIN' AND reason_code='TORRENT_START_UNCERTAIN') OR
       (phase='ACTIVE' AND reason_code='TORRENT_DOWNLOAD_IN_PROGRESS') OR
       (phase='COMPLETE' AND reason_code='TORRENT_DOWNLOAD_COMPLETE') OR
       (phase='REVIEW' AND reason_code IN ('TORRENT_TRANSFER_CONFLICT','TORRENT_PIECE_OVERLAP')))
);
CREATE TABLE IF NOT EXISTS job_operations (
 operation_id INTEGER PRIMARY KEY,
 state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(state IN ('ACTIVE','CANCEL_REQUESTED','CANCELED','STAGED')),
 cleanup_state TEXT NOT NULL DEFAULT 'NONE' CHECK(cleanup_state IN ('NONE','PENDING','REVIEW','COMPLETE')),
 reason_code TEXT,
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms),
 CHECK((state IN ('ACTIVE','STAGED') AND cleanup_state='NONE' AND reason_code IS NULL) OR
       (state IN ('CANCEL_REQUESTED','CANCELED') AND
        ((cleanup_state='PENDING' AND reason_code IN ('CANCELLATION_DRAINING','CANCELLATION_PENDING')) OR
         (cleanup_state='REVIEW' AND reason_code='CANCELLATION_REVIEW') OR
         (cleanup_state='COMPLETE' AND reason_code='CANCELLATION_COMPLETE'))))
);
CREATE TABLE IF NOT EXISTS job_operation_members (
 job_id INTEGER PRIMARY KEY REFERENCES jobs(job_id),
 operation_id INTEGER NOT NULL REFERENCES job_operations(operation_id)
);
CREATE INDEX IF NOT EXISTS idx_job_operation_members ON job_operation_members(operation_id);
CREATE TABLE IF NOT EXISTS cancellation_cleanup (
 plan_id INTEGER PRIMARY KEY REFERENCES acquisition_plans(plan_id),
 operation_id INTEGER NOT NULL REFERENCES job_operations(operation_id),
 fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^a-f0-9]*'),
 files_json TEXT NOT NULL CHECK(length(CAST(files_json AS BLOB)) BETWEEN 2 AND 1048576),
 phase TEXT NOT NULL CHECK(phase IN ('PREPARED','STOP_UNCERTAIN','REMOVE_UNCERTAIN','FILES','COMPLETE')),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms)
);
CREATE TABLE IF NOT EXISTS staging_cleanup_artifacts (
 artifact_id INTEGER PRIMARY KEY,
 operation_id INTEGER NOT NULL REFERENCES job_operations(operation_id),
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 root_path TEXT NOT NULL CHECK(length(root_path) BETWEEN 1 AND 4096),
 relative_path TEXT NOT NULL CHECK(length(relative_path) BETWEEN 1 AND 128),
 root_device INTEGER NOT NULL,
 root_inode INTEGER NOT NULL,
 file_device INTEGER NOT NULL,
 file_inode INTEGER NOT NULL,
 phase TEXT NOT NULL CHECK(phase IN ('TEMP','PUBLISH_UNCERTAIN','PUBLISHED','REMOVE_UNCERTAIN','REMOVED')),
 UNIQUE(root_path,relative_path)
);
CREATE TABLE IF NOT EXISTS management_actions (
 operation_id INTEGER NOT NULL REFERENCES job_operations(operation_id),
 action TEXT NOT NULL CHECK(action IN ('KEEP','CLEANUP','REJECT')),
 target_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 PRIMARY KEY(operation_id,action),
 CHECK(target_job_id<>job_id)
);

CREATE TABLE IF NOT EXISTS media_verifications (
 plan_id INTEGER PRIMARY KEY REFERENCES acquisition_plans(plan_id),
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 download_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 submission_fingerprint TEXT NOT NULL CHECK(length(submission_fingerprint)=64 AND submission_fingerprint NOT GLOB '*[^a-f0-9]*'),
 transfer_fingerprint TEXT NOT NULL CHECK(length(transfer_fingerprint)=64 AND transfer_fingerprint NOT GLOB '*[^a-f0-9]*'),
 evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json) AND length(CAST(evidence_json AS BLOB)) BETWEEN 1 AND 16384),
 verified_at_ms INTEGER NOT NULL CHECK(verified_at_ms>=0),
 CHECK(job_id<>download_job_id)
);
CREATE TABLE IF NOT EXISTS staging_publications (
 plan_id INTEGER PRIMARY KEY REFERENCES media_verifications(plan_id),
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 verification_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json) AND length(CAST(manifest_json AS BLOB)) BETWEEN 1 AND 65536),
 state TEXT NOT NULL CHECK(state IN ('PLANNED','PUBLISHED')),
 receipts_json TEXT CHECK(receipts_json IS NULL OR (json_valid(receipts_json) AND length(CAST(receipts_json AS BLOB))<=16384)),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 published_at_ms INTEGER CHECK(published_at_ms IS NULL OR published_at_ms>=created_at_ms),
 CHECK(job_id<>verification_job_id),
 CHECK((state='PLANNED' AND receipts_json IS NULL AND published_at_ms IS NULL) OR
       (state='PUBLISHED' AND receipts_json IS NOT NULL AND published_at_ms IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS anime_review_decisions (
 anidb_id INTEGER PRIMARY KEY REFERENCES identified_anime(anidb_id),
 operation_id INTEGER NOT NULL REFERENCES job_operations(operation_id),
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 decision TEXT NOT NULL CHECK(decision='REJECT'),
 cleanup_state TEXT NOT NULL CHECK(cleanup_state IN ('PENDING','COMPLETE')),
 decided_at_ms INTEGER NOT NULL CHECK(decided_at_ms>=0),
 reason_text TEXT CHECK(reason_text IS NULL OR
   (length(trim(reason_text)) BETWEEN 1 AND 500))
);

CREATE TABLE IF NOT EXISTS keep_imports (
 job_id INTEGER PRIMARY KEY REFERENCES jobs(job_id),
 phase TEXT NOT NULL CHECK(phase IN ('PLANNED','ADD_UNCERTAIN','SERIES_READY','IMPORT_UNCERTAIN','COMPLETE')),
 data_json TEXT NOT NULL CHECK(json_valid(data_json) AND length(CAST(data_json AS BLOB))<=65536),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=0)
);

CREATE TABLE IF NOT EXISTS batch_metadata_admissions (
 event_id INTEGER PRIMARY KEY REFERENCES incoming_events(event_id),
 verification_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^a-f0-9]*'),
 ownership_marker TEXT NOT NULL UNIQUE CHECK(length(ownership_marker)=32 AND ownership_marker NOT GLOB '*[^a-f0-9]*'),
 base_url TEXT NOT NULL CHECK(length(base_url) BETWEEN 1 AND 2048),
 save_path TEXT NOT NULL CHECK(length(save_path) BETWEEN 2 AND 4096),
 category TEXT NOT NULL CHECK(length(category) BETWEEN 1 AND 64),
 timeout_seconds INTEGER NOT NULL CHECK(timeout_seconds BETWEEN 1 AND 60),
 api_major INTEGER NOT NULL CHECK(api_major IN (4,5)),
 state TEXT NOT NULL CHECK(state IN ('UNCERTAIN','EXTERNAL','REVIEW','OWNED_STOPPED','ADOPTED','RELEASED')),
 inventory_json TEXT CHECK(inventory_json IS NULL OR json_valid(inventory_json)),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_metadata_live_hash
 ON batch_metadata_admissions(info_hash) WHERE state NOT IN ('RELEASED','EXTERNAL');

CREATE TABLE IF NOT EXISTS batch_verifications (
 batch_id INTEGER PRIMARY KEY,
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 identity_result_id INTEGER NOT NULL,
 anidb_id INTEGER NOT NULL CHECK(anidb_id>0),
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>0),
 release_sha256 TEXT NOT NULL CHECK(length(release_sha256)=64 AND release_sha256 NOT GLOB '*[^a-f0-9]*'),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^a-f0-9]*'),
 info_sha256 TEXT CHECK(length(info_sha256)=64 AND info_sha256 NOT GLOB '*[^a-f0-9]*'),
 verified_at_ms INTEGER NOT NULL CHECK(verified_at_ms>=0),
 FOREIGN KEY(identity_result_id,anidb_id) REFERENCES identity_results(result_id,anidb_id)
);
CREATE INDEX IF NOT EXISTS idx_batch_verifications_identity ON batch_verifications(identity_result_id);
CREATE TABLE IF NOT EXISTS batch_verified_files (
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
CREATE INDEX IF NOT EXISTS idx_batch_files_episode ON batch_verified_files(batch_id,tvdb_season,tvdb_episode);
CREATE TABLE IF NOT EXISTS batch_trial_plans (
 plan_id INTEGER PRIMARY KEY,
 batch_id INTEGER NOT NULL UNIQUE REFERENCES batch_verifications(batch_id),
 evaluation_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 anchor_episode INTEGER NOT NULL CHECK(anchor_episode>0),
 backfill_count INTEGER NOT NULL CHECK(backfill_count BETWEEN 0 AND 1000),
 following_count INTEGER NOT NULL CHECK(following_count BETWEEN 0 AND 1000),
 configuration_revision INTEGER NOT NULL CHECK(configuration_revision>0),
 max_episode_bytes INTEGER NOT NULL CHECK(max_episode_bytes>0),
 max_plan_bytes INTEGER NOT NULL CHECK(max_plan_bytes>0),
 planned_size_bytes INTEGER NOT NULL CHECK(planned_size_bytes>0 AND planned_size_bytes<=max_plan_bytes),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 CHECK(backfill_count>0 OR following_count>0)
);
CREATE TABLE IF NOT EXISTS batch_trial_files (
 plan_id INTEGER NOT NULL REFERENCES batch_trial_plans(plan_id),
 file_index INTEGER NOT NULL CHECK(file_index>=0),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0),
 selected_size_bytes INTEGER NOT NULL CHECK(selected_size_bytes>0),
 PRIMARY KEY(plan_id,file_index),
 UNIQUE(plan_id,tvdb_episode)
);
CREATE INDEX IF NOT EXISTS idx_batch_trial_by_episode ON batch_trial_files(plan_id,tvdb_episode);
CREATE TABLE IF NOT EXISTS batch_episode_reservations (
 reservation_id INTEGER PRIMARY KEY,
 plan_id INTEGER NOT NULL REFERENCES batch_trial_plans(plan_id),
 owner_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 file_index INTEGER NOT NULL CHECK(file_index>=0),
 anidb_id INTEGER NOT NULL REFERENCES identified_anime(anidb_id),
 anidb_episode_id INTEGER NOT NULL CHECK(anidb_episode_id>0),
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>0),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0),
 tvdb_episode_id INTEGER NOT NULL CHECK(tvdb_episode_id>0),
 state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(state IN ('ACTIVE','RELEASED')),
 reserved_at_ms INTEGER NOT NULL CHECK(reserved_at_ms>=0),
 ended_at_ms INTEGER,
 ended_by_job_id INTEGER REFERENCES jobs(job_id),
 UNIQUE(plan_id,file_index),
 UNIQUE(plan_id,tvdb_episode),
 FOREIGN KEY(plan_id,file_index) REFERENCES batch_trial_files(plan_id,file_index),
 CHECK((state='ACTIVE' AND ended_at_ms IS NULL AND ended_by_job_id IS NULL) OR
       (state='RELEASED' AND ended_at_ms>=reserved_at_ms AND ended_by_job_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_active_anidb ON batch_episode_reservations(anidb_episode_id) WHERE state='ACTIVE';
CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_active_tvdb ON batch_episode_reservations(tvdb_episode_id) WHERE state='ACTIVE';
CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_active_placement ON batch_episode_reservations(tvdb_series_id,tvdb_season,tvdb_episode) WHERE state='ACTIVE';
CREATE INDEX IF NOT EXISTS idx_batch_owner ON batch_episode_reservations(owner_job_id,state);
CREATE TABLE IF NOT EXISTS batch_torrent_submissions (
 plan_id INTEGER PRIMARY KEY REFERENCES batch_trial_plans(plan_id),
 reservation_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 first_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 first_attempt INTEGER NOT NULL CHECK(first_attempt>0),
 info_hash TEXT NOT NULL UNIQUE CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^a-f0-9]*'),
 info_sha256 TEXT CHECK(length(info_sha256)=64 AND info_sha256 NOT GLOB '*[^a-f0-9]*'),
 ownership_marker TEXT NOT NULL UNIQUE CHECK(length(ownership_marker)=32 AND ownership_marker NOT GLOB '*[^a-f0-9]*'),
 base_url TEXT NOT NULL CHECK(length(base_url) BETWEEN 1 AND 2048),
 save_path TEXT NOT NULL CHECK(length(save_path) BETWEEN 2 AND 4096),
 category TEXT NOT NULL CHECK(length(category) BETWEEN 1 AND 64),
 timeout_seconds INTEGER NOT NULL CHECK(timeout_seconds BETWEEN 1 AND 60),
 api_major INTEGER NOT NULL CHECK(api_major IN (4,5)),
 fingerprint TEXT NOT NULL UNIQUE CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^a-f0-9]*'),
 configuration_revision INTEGER NOT NULL CHECK(configuration_revision>0),
 state TEXT NOT NULL CHECK(state IN ('UNCERTAIN','EXTERNAL','REVIEW','OWNED_STOPPED')),
 reason_code TEXT NOT NULL CHECK(
   (state='UNCERTAIN' AND reason_code='TORRENT_SUBMISSION_UNCERTAIN') OR
   (state='EXTERNAL' AND reason_code='TORRENT_ALREADY_PRESENT') OR
   (state='REVIEW' AND reason_code='TORRENT_PROVENANCE_CONFLICT') OR
   (state='OWNED_STOPPED' AND reason_code='TORRENT_STOPPED_CONFIRMED')),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms)
);
CREATE INDEX IF NOT EXISTS idx_batch_submission_job ON batch_torrent_submissions(reservation_job_id);
CREATE TABLE IF NOT EXISTS batch_torrent_transfers (
 plan_id INTEGER PRIMARY KEY REFERENCES batch_torrent_submissions(plan_id),
 transfer_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 transfer_attempt INTEGER NOT NULL CHECK(transfer_attempt>0),
 selected_index INTEGER NOT NULL CHECK(selected_index>=0),
 piece_size INTEGER NOT NULL CHECK(piece_size BETWEEN 1 AND 67108864),
 fingerprint TEXT NOT NULL UNIQUE CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^a-f0-9]*'),
 phase TEXT NOT NULL CHECK(phase IN ('SELECT_UNCERTAIN','SELECTED','START_UNCERTAIN','ACTIVE','COMPLETE','REVIEW')),
 reason_code TEXT NOT NULL,
 start_job_id INTEGER REFERENCES jobs(job_id),
 start_attempt INTEGER CHECK(start_attempt IS NULL OR start_attempt>0),
 selected_progress REAL NOT NULL DEFAULT 0 CHECK(selected_progress BETWEEN 0 AND 1),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms),
 CHECK((start_job_id IS NULL AND start_attempt IS NULL) OR (start_job_id IS NOT NULL AND start_attempt IS NOT NULL)),
 CHECK(phase NOT IN ('START_UNCERTAIN','ACTIVE') OR start_job_id IS NOT NULL),
 CHECK((phase='SELECT_UNCERTAIN' AND reason_code='TORRENT_SELECTION_UNCERTAIN') OR
       (phase='SELECTED' AND reason_code='TORRENT_SELECTION_CONFIRMED') OR
       (phase='START_UNCERTAIN' AND reason_code='TORRENT_START_UNCERTAIN') OR
       (phase='ACTIVE' AND reason_code='TORRENT_DOWNLOAD_IN_PROGRESS') OR
       (phase='COMPLETE' AND reason_code='TORRENT_DOWNLOAD_COMPLETE') OR
       (phase='REVIEW' AND reason_code IN ('TORRENT_TRANSFER_CONFLICT','TORRENT_PIECE_OVERLAP')))
);
CREATE TABLE IF NOT EXISTS batch_probe_media (
 plan_id INTEGER PRIMARY KEY REFERENCES batch_torrent_transfers(plan_id),
 verification_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 transfer_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 submission_fingerprint TEXT NOT NULL CHECK(length(submission_fingerprint)=64 AND submission_fingerprint NOT GLOB '*[^a-f0-9]*'),
 transfer_fingerprint TEXT NOT NULL CHECK(length(transfer_fingerprint)=64 AND transfer_fingerprint NOT GLOB '*[^a-f0-9]*'),
 evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json) AND length(CAST(evidence_json AS BLOB))<=65536),
 verified_at_ms INTEGER NOT NULL CHECK(verified_at_ms>=0)
);
CREATE TABLE IF NOT EXISTS batch_torrent_unlocks (
 plan_id INTEGER PRIMARY KEY REFERENCES batch_probe_media(plan_id),
 unlock_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 unlock_attempt INTEGER NOT NULL CHECK(unlock_attempt>0),
 verification_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 fingerprint TEXT NOT NULL UNIQUE CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^a-f0-9]*'),
 priorities_json TEXT NOT NULL CHECK(json_valid(priorities_json) AND length(CAST(priorities_json AS BLOB))<=65536),
 piece_size INTEGER NOT NULL CHECK(piece_size BETWEEN 1 AND 67108864),
 phase TEXT NOT NULL CHECK(phase IN ('SELECT_UNCERTAIN','SELECTED','START_UNCERTAIN','ACTIVE','COMPLETE','REVIEW')),
 reason_code TEXT NOT NULL,
 start_job_id INTEGER REFERENCES jobs(job_id),
 start_attempt INTEGER CHECK(start_attempt IS NULL OR start_attempt>0),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms),
 CHECK((start_job_id IS NULL AND start_attempt IS NULL) OR (start_job_id IS NOT NULL AND start_attempt IS NOT NULL)),
 CHECK(phase NOT IN ('START_UNCERTAIN','ACTIVE') OR start_job_id IS NOT NULL),
 CHECK((phase='SELECT_UNCERTAIN' AND reason_code='TORRENT_SELECTION_UNCERTAIN') OR
       (phase='SELECTED' AND reason_code='TORRENT_SELECTION_CONFIRMED') OR
       (phase='START_UNCERTAIN' AND reason_code='TORRENT_START_UNCERTAIN') OR
       (phase='ACTIVE' AND reason_code='TORRENT_DOWNLOAD_IN_PROGRESS') OR
       (phase='COMPLETE' AND reason_code='TORRENT_DOWNLOAD_COMPLETE') OR
       (phase='REVIEW' AND reason_code IN ('TORRENT_TRANSFER_CONFLICT','TORRENT_PIECE_OVERLAP')))
);
CREATE TABLE IF NOT EXISTS batch_probe_stops (
 plan_id INTEGER PRIMARY KEY REFERENCES batch_probe_media(plan_id),
 stop_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 stop_attempt INTEGER NOT NULL CHECK(stop_attempt>0),
 fingerprint TEXT NOT NULL UNIQUE CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^a-f0-9]*'),
 phase TEXT NOT NULL CHECK(phase IN ('STOP_UNCERTAIN','STOPPED')),
 piece_size INTEGER NOT NULL CHECK(piece_size BETWEEN 1 AND 67108864),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms)
);
CREATE TABLE IF NOT EXISTS batch_episode_media (
 plan_id INTEGER NOT NULL,
 file_index INTEGER NOT NULL,
 verification_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 unlock_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 submission_fingerprint TEXT NOT NULL CHECK(length(submission_fingerprint)=64),
 transfer_fingerprint TEXT NOT NULL CHECK(length(transfer_fingerprint)=64),
 evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json) AND length(CAST(evidence_json AS BLOB))<=65536),
 verified_at_ms INTEGER NOT NULL CHECK(verified_at_ms>=0),
 PRIMARY KEY(plan_id,file_index),
 FOREIGN KEY(plan_id,file_index) REFERENCES batch_trial_files(plan_id,file_index),
 FOREIGN KEY(plan_id) REFERENCES batch_torrent_unlocks(plan_id)
);

CREATE TABLE IF NOT EXISTS batch_staging_publications (
 plan_id INTEGER NOT NULL,
 file_index INTEGER NOT NULL,
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 verification_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json) AND length(CAST(manifest_json AS BLOB)) BETWEEN 1 AND 65536),
 state TEXT NOT NULL CHECK(state IN ('PLANNED','PUBLISHED')),
 receipts_json TEXT CHECK(receipts_json IS NULL OR
  (json_valid(receipts_json) AND length(CAST(receipts_json AS BLOB))<=16384)),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 published_at_ms INTEGER CHECK(published_at_ms IS NULL OR published_at_ms>=created_at_ms),
 PRIMARY KEY(plan_id,file_index),
 FOREIGN KEY(plan_id,file_index) REFERENCES batch_episode_media(plan_id,file_index),
 CHECK((state='PLANNED' AND receipts_json IS NULL AND published_at_ms IS NULL) OR
       (state='PUBLISHED' AND receipts_json IS NOT NULL AND published_at_ms IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS batch_keep_imports (
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 plan_id INTEGER NOT NULL,
 file_index INTEGER NOT NULL,
 phase TEXT NOT NULL CHECK(phase IN ('PLANNED','ADD_UNCERTAIN','SERIES_READY','IMPORT_UNCERTAIN','COMPLETE')),
 data_json TEXT NOT NULL CHECK(json_valid(data_json) AND length(CAST(data_json AS BLOB)) BETWEEN 1 AND 65536),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=0),
 PRIMARY KEY(job_id,file_index),
 UNIQUE(job_id,plan_id,file_index),
 FOREIGN KEY(plan_id,file_index) REFERENCES batch_staging_publications(plan_id,file_index)
);
CREATE TABLE IF NOT EXISTS batch_download_cleanup (
 plan_id INTEGER PRIMARY KEY REFERENCES batch_torrent_submissions(plan_id),
 decision_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 operation_id INTEGER NOT NULL REFERENCES job_operations(operation_id),
 fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^a-f0-9]*'),
 files_json TEXT NOT NULL CHECK(json_valid(files_json) AND length(CAST(files_json AS BLOB))<=1048576),
 phase TEXT NOT NULL CHECK(phase IN ('PREPARED','STOP_UNCERTAIN','REMOVE_UNCERTAIN','FILES','COMPLETE')),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms)
);
CREATE TABLE IF NOT EXISTS batch_keep_cleanup (
 job_id INTEGER PRIMARY KEY REFERENCES jobs(job_id),
 decision_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 phase TEXT NOT NULL CHECK(phase IN ('PENDING','COMPLETE')),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms)
);
CREATE TABLE IF NOT EXISTS batch_submission_inventories (
 plan_id INTEGER PRIMARY KEY REFERENCES batch_torrent_submissions(plan_id),
 inventory_json TEXT NOT NULL CHECK(json_valid(inventory_json) AND length(CAST(inventory_json AS BLOB)) BETWEEN 1 AND 1048576),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0)
);
CREATE TABLE IF NOT EXISTS batch_cancellation_cleanup (
 plan_id INTEGER PRIMARY KEY REFERENCES batch_torrent_submissions(plan_id),
 operation_id INTEGER NOT NULL REFERENCES job_operations(operation_id),
 fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^a-f0-9]*'),
 files_json TEXT NOT NULL CHECK(json_valid(files_json) AND length(CAST(files_json AS BLOB))<=1048576),
 phase TEXT NOT NULL CHECK(phase IN ('PREPARED','STOP_UNCERTAIN','REMOVE_UNCERTAIN','FILES','COMPLETE')),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms)
);
CREATE TABLE IF NOT EXISTS sonarr_backfill_results (
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0 AND tvdb_episode<=1000),
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 sonarr_episode_id INTEGER CHECK(sonarr_episode_id>0),
 tvdb_episode_id INTEGER CHECK(tvdb_episode_id>0),
 status TEXT NOT NULL CHECK(status IN ('IN_PROGRESS','NOT_LISTED','IDENTITY_UNKNOWN','IDENTITY_CONFLICT','HAS_FILE','CANDIDATES','NO_RESULTS','NOT_AIRED','MAPPING_MISSING','WAITING_FOR_DUB')),
 candidates INTEGER NOT NULL CHECK(candidates BETWEEN 0 AND 1000),
 approved INTEGER NOT NULL CHECK(approved BETWEEN 0 AND candidates),
 exact INTEGER NOT NULL CHECK(exact BETWEEN 0 AND candidates),
 observed_at_ms INTEGER NOT NULL CHECK(observed_at_ms>=0),
 PRIMARY KEY(plan_id,tvdb_episode)
);
CREATE TABLE IF NOT EXISTS sonarr_episode_selections (
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0),
 tvdb_episode_id INTEGER NOT NULL CHECK(tvdb_episode_id>0),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^0-9a-f]*'),
 selected_at_ms INTEGER NOT NULL CHECK(selected_at_ms>=0),
 PRIMARY KEY(plan_id,tvdb_episode),
 UNIQUE(plan_id,info_hash)
);

CREATE TABLE IF NOT EXISTS sonarr_episode_candidates (
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0 AND tvdb_episode<=1000),
 rank INTEGER NOT NULL CHECK(rank>0 AND rank<=1000),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^0-9a-f]*'),
 state TEXT NOT NULL DEFAULT 'READY' CHECK(state IN ('READY','ACTIVE','FAILED','SUCCEEDED')),
 reason_code TEXT,
 PRIMARY KEY(plan_id,tvdb_episode,rank),
 UNIQUE(plan_id,tvdb_episode,info_hash)
);

CREATE TABLE IF NOT EXISTS sonarr_magnet_intents (
 event_id INTEGER PRIMARY KEY REFERENCES incoming_events(event_id),
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode BETWEEN 1 AND 1000),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^0-9a-f]*'),
 marker TEXT NOT NULL UNIQUE CHECK(length(marker)=32 AND marker NOT GLOB '*[^0-9a-f]*'),
 api_major INTEGER NOT NULL CHECK(api_major IN (4,5)),
 base_url TEXT NOT NULL,
 save_path TEXT NOT NULL,
 category TEXT NOT NULL,
 timeout_seconds INTEGER NOT NULL CHECK(timeout_seconds BETWEEN 1 AND 60),
 inventory_json TEXT CHECK(inventory_json IS NULL OR json_valid(inventory_json)),
 state TEXT NOT NULL DEFAULT 'UNCERTAIN' CHECK(state IN ('UNCERTAIN','OWNED_STOPPED','REVIEW','EXTERNAL','RELEASED')),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=0)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_magnet_active_hash ON sonarr_magnet_intents(info_hash) WHERE state<>'RELEASED';

CREATE TABLE IF NOT EXISTS prowlarr_search_snapshots (
 snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 attempt INTEGER NOT NULL CHECK(attempt >= 0),
 captured_at_ms INTEGER NOT NULL CHECK(captured_at_ms >= 0),
 plan_id INTEGER CHECK(plan_id IS NULL OR plan_id > 0),
 tvdb_series_id INTEGER CHECK(tvdb_series_id IS NULL OR tvdb_series_id > 0),
 season INTEGER CHECK(season IS NULL OR season >= 0),
 episode INTEGER CHECK(episode IS NULL OR episode > 0),
 search_mode TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('COMPLETE', 'PARTIAL', 'ERROR')),
 snapshot_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prowlarr_snapshots_job ON prowlarr_search_snapshots(job_id, attempt);
CREATE INDEX IF NOT EXISTS idx_prowlarr_snapshots_plan ON prowlarr_search_snapshots(plan_id, episode);

"""

def connect(path):
    db = sqlite3.connect(path, timeout=5)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA journal_mode=WAL")
    return db

@contextmanager
def admitted_connection(gate):
    """New runtime operations use admission, then open/close their connection.

    For an entire job attempt, hold gate.operation() around handler execution
    instead; that admission also covers short-lived connections and heartbeats.
    Low-level connect() is retained for explicit bootstrap and offline fixtures.
    Never nest admissions or use this during exclusive migration maintenance.
    """
    with gate.operation():
        from src.nextgen.storage.migrations import _open, LATEST_VERSION, MigrationError, MigrationUnsupported
        try:
            db = _open(gate.database, readonly=False,timeout_ms=5000)
        except (OSError, sqlite3.Error):
            raise MigrationError() from None
        try:
            try:
                if db.execute("PRAGMA user_version").fetchone()[0] != LATEST_VERSION:
                    raise MigrationUnsupported()
                db.execute("PRAGMA busy_timeout=5000")
                db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                raise MigrationError() from None
            yield db
        finally:
            db.close()


def initialize(db):
    """Initialize a *new* NextGen database at schema v54; never implicitly upgrade.

    Existing v1/v2/v3/v4/v5/v6/v7/v8/v9/v10/v11/v12/v13/v14/v15/v16/v17/v18/v19/v20/v21/v22 databases require explicit versioned SQL migrations
    before accessing v50 storage.
    """
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 54:
        return
    if version != 0:
        raise ValueError("existing NextGen database requires versioned SQL upgrade")
    present = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone()
    if present:
        raise ValueError("database has tables but no schema version; refuse implicit upgrade")
    if db.in_transaction:
        raise ValueError("initialization requires no active transaction")
    try:
        db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nPRAGMA user_version=54;\nCOMMIT;")
    except BaseException:
        db.rollback()
        raise


@contextmanager
def transaction(db):
    """Own one atomic unit; reject nesting rather than committing caller work."""
    if db.in_transaction:
        raise ValueError("requires a connection outside an active transaction")
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _delivery(source, raw_body):
    if type(source) is not str or not source.strip():
        raise ValueError("source required")
    if type(raw_body) is not bytes:
        raise TypeError("raw_body must be bytes")


def _log(record):
    log_history(logging.getLogger("nextgen." + record.component), record)


def accept_event(db, source, raw_body, received_at_ms=None):
    """Every delivery receives its own durable work record."""
    _delivery(source, raw_body)
    now = integer(int(time.time()*1000) if received_at_ms is None else received_at_ms)
    with transaction(db):
        event_id = db.execute(
            "INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES(?,?,?)",
            (source,raw_body,now)).lastrowid
        audit = append_event_history(db, event_id=event_id, event_code="ACCEPTED",
                                     component="intake", occurred_at_ms=now)
    _log(audit)
    return event_id

class IdentityConflict(ValueError):
    reason_code = "IDENTITY_CONFLICT"

    def __init__(self):
        super().__init__("Conflicting anime information needs review.")


class IdentityNotReady(ValueError):
    reason_code = "IDENTITY_INCOMPLETE"

    def __init__(self):
        super().__init__("Anime identification is incomplete.")


def persist_identity(db, event_id, candidate, validated_at_ms=None):
    """Return result ID/audit inside caller transaction; never commit or log.

    Worker adapters must hold/fence their job lease. Existing incoming-event
    leases are deliberately not bypassed by this job-based persistence path.
    """
    if not db.in_transaction:
        raise ValueError("identity persistence requires an active transaction")
    from src.nextgen.schema.anime_series import AnimeSeries
    validated = AnimeSeries.from_dict(candidate)
    payload = validated.to_dict()
    anime = payload["anime"]
    aid = anime["anidb_id"]
    now = integer(int(time.time() * 1000) if validated_at_ms is None else validated_at_ms)
    integer(event_id, positive=True)
    event = db.execute("SELECT state,lease_token,lease_expires_at_ms FROM incoming_events WHERE event_id=?", (event_id,)).fetchone()
    if event is None:
        raise IdentityNotReady()
    existing_result = db.execute("SELECT result_id,series_json FROM identity_results WHERE event_id=? AND anidb_id=?", (event_id,aid)).fetchone()
    if existing_result is not None:
        if json.loads(existing_result[1]) != payload:
            raise IdentityConflict()
        return existing_result[0], None
    if event[0] not in ("PENDING","RUNNING","RETRY_WAIT"):
        raise IdentityNotReady()
    if event[0] == "RUNNING":
        raise IdentityNotReady()
    existing = db.execute(
        "SELECT tvdb_series_id,title FROM identified_anime WHERE anidb_id=?",
        (aid,)).fetchone()
    if existing and existing[0] != anime["tvdb_series_id"]:
        raise IdentityConflict()
    if existing and existing[1] != anime["title"]:
        db.execute("UPDATE identified_anime SET title=? WHERE anidb_id=?", (anime["title"],aid))
    db.execute(
        "INSERT OR IGNORE INTO identified_anime VALUES(?,?,?)",
        (aid,anime["tvdb_series_id"],anime["title"]))
    upsert_anime_titles(db, aid, anime.get("english_title"),
                       payload.get("attributes", {}).get("romaji_title"))
    rid = db.execute(
        "INSERT INTO identity_results(event_id,anidb_id,schema_version,series_json,validated_at_ms)"
        " VALUES(?,?,?,?,?)",
        (event_id,aid,2,json.dumps(payload,sort_keys=True),now)).lastrowid
    for episode in payload["episodes"]:
        eid = episode["anidb_episode_id"]
        owner = db.execute(
            "SELECT anidb_id FROM identified_episodes WHERE anidb_episode_id=?",
            (eid,)).fetchone()
        if owner and owner[0] != aid:
            raise IdentityConflict()
        db.execute(
            "INSERT OR IGNORE INTO identified_episodes VALUES(?,?)",(eid,aid))
        db.execute(
            "INSERT INTO result_episode_links VALUES(?,?,?)",(rid,aid,eid))
    db.execute("UPDATE incoming_events SET state='IDENTIFIED', next_run_at_ms=NULL, lease_token=NULL, lease_expires_at_ms=NULL, last_reason_code=NULL WHERE event_id=?", (event_id,))
    audit = append_event_history(db, event_id=event_id, event_code="SERIES_IDENTIFIED",
                                 component="identity", occurred_at_ms=now)
    return rid, audit


def upsert_anime_titles(db, anidb_id, english_title, romaji_title):
    """Preserve known translations; never overwrite a verified identity or alias."""
    if not english_title and not romaji_title:
        return
    for title in (english_title, romaji_title):
        if title is not None and (type(title) is not str or not 1 <= len(title.strip()) <= 512):
            raise ValueError("Invalid translated anime title")
    db.execute(
        "INSERT INTO anime_title_metadata(anidb_id,english_title,romaji_title) VALUES(?,?,?) "
        "ON CONFLICT(anidb_id) DO UPDATE SET "
        "english_title=COALESCE(anime_title_metadata.english_title,excluded.english_title), "
        "romaji_title=COALESCE(anime_title_metadata.romaji_title,excluded.romaji_title)",
        (anidb_id, english_title, romaji_title))


def save_identity(db, event_id, candidate, validated_at_ms=None):
    """Store validated AnimeSeries v2; zero episodes are allowed."""
    with transaction(db):
        rid, audit = persist_identity(db,event_id,candidate,validated_at_ms)
    if audit is not None:
        _log(audit)
    return rid


def _enqueue_job(db, *, kind, payload, component, event_id=None,
                 created_by_job_id=None, available_at_ms=None, operation_id=None):
    if not db.in_transaction:
        raise ValueError("enqueue requires an active transaction")
    component_name(component)
    if type(kind) is not str or not kind.strip():
        raise ValueError("job kind required")
    if type(payload) is not dict:
        raise ValueError("job payload must be an object")
    raw = json.dumps(payload, allow_nan=False, sort_keys=True)
    if len(raw.encode("utf-8")) > 65536:
        raise ValueError("job payload exceeds the storage bound")
    for reference in (event_id, created_by_job_id):
        if reference is not None:
            integer(reference, positive=True)
    # Source references cannot create another consumer after cancellation has
    # fenced the referenced operation. These are explicit payload contracts.
    for key in ('preparation_job_id','evaluation_job_id','verification_job_id'):
        reference=payload.get(key)
        if type(reference) is int and db.execute(
                "SELECT 1 FROM job_operation_members m JOIN job_operations o USING(operation_id) "
                "WHERE m.job_id=? AND o.state<>'ACTIVE'",(reference,)).fetchone():
            raise ValueError('Referenced work is no longer accepting jobs.')
    now = int(time.time() * 1000)
    available = integer(now if available_at_ms is None else available_at_ms)
    job_id = db.execute(
        """INSERT INTO jobs(event_id,created_by_job_id,kind,payload_json,
          available_at_ms,created_at_ms,updated_at_ms)
          VALUES(?,?,?,?,?,?,?)""",
        (event_id,created_by_job_id,kind,raw,available,now,now)
    ).lastrowid
    if operation_id is None:
        operation_id=db.execute('INSERT INTO job_operations(created_at_ms,updated_at_ms) VALUES(?,?)',(now,now)).lastrowid
    else:
        integer(operation_id,positive=True)
        if db.execute("SELECT state FROM job_operations WHERE operation_id=?",(operation_id,)).fetchone() != ('ACTIVE',):
            raise ValueError('Operation is no longer accepting work.')
    db.execute('INSERT INTO job_operation_members VALUES(?,?)',(job_id,operation_id))
    audit = append_job_history(db, job_id=job_id, event_code="CREATED",
                               component=component, occurred_at_ms=now,
                               details={"attempt": 0})
    return job_id, audit


def enqueue_job_with_receipt(db, **kwargs):
    """Worker-owned handoff: return ID and audit receipt; no commit or logging."""
    return _enqueue_job(db, **kwargs)


def enqueue_job(db, *, kind, payload, component, event_id=None,
                created_by_job_id=None, available_at_ms=None, operation_id=None):
    """Insert independent work and CREATED audit inside an active transaction.

    No commit or logging. Caller owns rollback and may emit CREATED with event()
    after commit. Use transaction(db), or explicit BEGIN/commit/rollback.
    component is the actual creating component, not a dependency relationship.
    """
    return _enqueue_job(db, kind=kind, payload=payload, component=component,
                        event_id=event_id, created_by_job_id=created_by_job_id,
                        available_at_ms=available_at_ms, operation_id=operation_id)[0]


def accept_event_and_job(db, source, raw_body, *, kind, payload, component="intake", logger=None):
    """Commit delivery, PENDING job and both audit rows before acknowledgment."""
    _delivery(source, raw_body)
    now = int(time.time()*1000)
    with transaction(db):
        event_id = db.execute(
            "INSERT INTO incoming_events(source,raw_body,received_at_ms) VALUES(?,?,?)",
            (source,raw_body,now)).lastrowid
        event_audit = append_event_history(db, event_id=event_id, event_code="ACCEPTED",
                                          component=component, occurred_at_ms=now)
        job_id, job_audit = _enqueue_job(db, kind=kind, payload=payload,
                                       component=component, event_id=event_id)
    if logger is None:
        _log(event_audit);_log(job_audit)
    else:
        log_history(logger,event_audit);log_history(logger,job_audit)
    return event_id, job_id
