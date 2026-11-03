-- v56 -> v57: Allow acquiring and staging episodes without TVDB placement.
-- Release verifications, episode reservations, and candidate selections
-- permit nullable TVDB placement (tvdb_season, tvdb_episode, tvdb_episode_id).
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v57_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v57_guard SELECT user_version=56 FROM pragma_user_version;

CREATE TABLE release_verifications_v57 (
 verification_id INTEGER PRIMARY KEY,
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 identity_result_id INTEGER NOT NULL,
 anidb_id INTEGER NOT NULL,
 release_sha256 TEXT NOT NULL CHECK(length(release_sha256)=64 AND release_sha256 NOT GLOB '*[^a-f0-9]*'),
 verification_scope TEXT NOT NULL CHECK(verification_scope='DECLARED_SINGLE_EPISODE'),
 anidb_episode_number INTEGER NOT NULL CHECK(anidb_episode_number>0),
 anidb_episode_id INTEGER NOT NULL,
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER CHECK(tvdb_season IS NULL OR tvdb_season>=0),
 tvdb_episode INTEGER CHECK(tvdb_episode IS NULL OR tvdb_episode>0),
 tvdb_episode_id INTEGER CHECK(tvdb_episode_id IS NULL OR tvdb_episode_id>0),
 tvdb_order TEXT NOT NULL DEFAULT 'official' CHECK(tvdb_order='official'),
 verified_at_ms INTEGER NOT NULL CHECK(verified_at_ms>=0),
 CHECK((tvdb_season IS NULL AND tvdb_episode IS NULL AND tvdb_episode_id IS NULL) OR
       (tvdb_season IS NOT NULL AND tvdb_episode IS NOT NULL AND tvdb_episode_id IS NOT NULL)),
 FOREIGN KEY(identity_result_id,anidb_id) REFERENCES identity_results(result_id,anidb_id),
 FOREIGN KEY(anidb_episode_id,anidb_id) REFERENCES identified_episodes(anidb_episode_id,anidb_id)
);
INSERT INTO release_verifications_v57 SELECT verification_id,job_id,identity_result_id,anidb_id,
 release_sha256,verification_scope,anidb_episode_number,anidb_episode_id,tvdb_series_id,
 tvdb_season,tvdb_episode,tvdb_episode_id,tvdb_order,verified_at_ms
 FROM release_verifications;

CREATE TABLE episode_reservations_v57 (
 reservation_id INTEGER PRIMARY KEY,
 owner_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 evaluation_job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 verification_id INTEGER NOT NULL REFERENCES release_verifications(verification_id),
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 anidb_id INTEGER NOT NULL REFERENCES identified_anime(anidb_id),
 anidb_episode_id INTEGER NOT NULL REFERENCES identified_episodes(anidb_episode_id),
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER CHECK(tvdb_season IS NULL OR tvdb_season>=0),
 tvdb_episode INTEGER CHECK(tvdb_episode IS NULL OR tvdb_episode>0),
 tvdb_episode_id INTEGER CHECK(tvdb_episode_id IS NULL OR tvdb_episode_id>0),
 tvdb_order TEXT NOT NULL DEFAULT 'official' CHECK(tvdb_order='official'),
 reserved_at_ms INTEGER NOT NULL CHECK(reserved_at_ms>=0),
 state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(state IN ('ACTIVE','RELEASED')),
 ended_at_ms INTEGER CHECK(ended_at_ms IS NULL OR ended_at_ms>=0),
 ended_by_job_id INTEGER REFERENCES jobs(job_id),
 CHECK((state='ACTIVE' AND ended_at_ms IS NULL AND ended_by_job_id IS NULL) OR
       (state='RELEASED' AND ended_at_ms IS NOT NULL AND ended_by_job_id IS NOT NULL)),
 CHECK((tvdb_season IS NULL AND tvdb_episode IS NULL AND tvdb_episode_id IS NULL) OR
       (tvdb_season IS NOT NULL AND tvdb_episode IS NOT NULL AND tvdb_episode_id IS NOT NULL))
);
INSERT INTO episode_reservations_v57 SELECT reservation_id,owner_job_id,evaluation_job_id,
 verification_id,plan_id,anidb_id,anidb_episode_id,tvdb_series_id,tvdb_season,tvdb_episode,
 tvdb_episode_id,tvdb_order,reserved_at_ms,state,ended_at_ms,ended_by_job_id
 FROM episode_reservations;

CREATE TABLE sonarr_episode_selections_v57 (
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 anidb_episode_number INTEGER NOT NULL CHECK(anidb_episode_number>0),
 tvdb_season INTEGER CHECK(tvdb_season IS NULL OR tvdb_season>=0),
 tvdb_episode INTEGER CHECK(tvdb_episode IS NULL OR tvdb_episode>0),
 tvdb_episode_id INTEGER CHECK(tvdb_episode_id IS NULL OR tvdb_episode_id>0),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^0-9a-f]*'),
 selected_at_ms INTEGER NOT NULL CHECK(selected_at_ms>=0),
 PRIMARY KEY(plan_id,anidb_episode_number),
 UNIQUE(plan_id,info_hash),
 CHECK((tvdb_season IS NULL AND tvdb_episode IS NULL AND tvdb_episode_id IS NULL) OR
       (tvdb_season IS NOT NULL AND tvdb_episode IS NOT NULL AND tvdb_episode_id IS NOT NULL))
);
INSERT INTO sonarr_episode_selections_v57 SELECT plan_id,anidb_episode_number,tvdb_season,
 tvdb_episode,tvdb_episode_id,info_hash,selected_at_ms
 FROM sonarr_episode_selections;

-- Ensure all rows are preserved
INSERT OR ROLLBACK INTO nextgen_v57_guard SELECT
 (SELECT count(*) FROM release_verifications_v57)=(SELECT count(*) FROM release_verifications)
 AND (SELECT count(*) FROM episode_reservations_v57)=(SELECT count(*) FROM episode_reservations)
 AND (SELECT count(*) FROM sonarr_episode_selections_v57)=(SELECT count(*) FROM sonarr_episode_selections);

DROP TABLE sonarr_episode_selections;
DROP TABLE episode_reservations;
DROP TABLE release_verifications;

ALTER TABLE release_verifications_v57 RENAME TO release_verifications;
ALTER TABLE episode_reservations_v57 RENAME TO episode_reservations;
ALTER TABLE sonarr_episode_selections_v57 RENAME TO sonarr_episode_selections;

CREATE INDEX IF NOT EXISTS idx_release_identity ON release_verifications(identity_result_id);
CREATE UNIQUE INDEX idx_episode_active_anidb ON episode_reservations(anidb_episode_id) WHERE state='ACTIVE';
CREATE UNIQUE INDEX idx_episode_active_tvdb ON episode_reservations(tvdb_episode_id) WHERE state='ACTIVE' AND tvdb_episode_id IS NOT NULL;
CREATE UNIQUE INDEX idx_episode_active_placement ON episode_reservations(tvdb_series_id,tvdb_season,tvdb_episode,tvdb_order) WHERE state='ACTIVE' AND tvdb_season IS NOT NULL;

INSERT OR ROLLBACK INTO nextgen_v57_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v57_guard;
PRAGMA user_version=57;
COMMIT;
PRAGMA foreign_keys=ON;
