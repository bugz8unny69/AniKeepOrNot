-- v55 -> v56: AniDB-first single-episode trial selection.
-- One trial plan per AniDB anime, anchored at the verified AniDB episode
-- number. Plan-scoped episode keys become AniDB episode numbers; each
-- selection keeps its own verified TVDB placement. Conversion uses only
-- stored, verified evidence. Search observations that cannot be converted
-- are derived data and are dropped; anything else unconvertible aborts.
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v56_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v56_guard SELECT user_version=55 FROM pragma_user_version;
-- Never merge plans.
INSERT OR ROLLBACK INTO nextgen_v56_guard SELECT count(*)=0 FROM
 (SELECT anidb_id FROM trial_plans GROUP BY anidb_id HAVING count(*)>1);
-- Every anchor converts through its own verification.
INSERT OR ROLLBACK INTO nextgen_v56_guard SELECT count(*)=0 FROM trial_plans p
 LEFT JOIN release_verifications v ON v.verification_id=p.anchor_verification_id
 WHERE v.verification_id IS NULL OR v.anidb_id<>p.anidb_id OR v.tvdb_series_id<>p.tvdb_series_id
 OR v.tvdb_season<>p.tvdb_season OR v.tvdb_episode<>p.anchor_episode;

CREATE TEMP TABLE nextgen_v56_keys(plan_id INTEGER, tvdb_episode INTEGER, required INTEGER);
INSERT INTO nextgen_v56_keys SELECT plan_id,tvdb_episode,0 FROM sonarr_backfill_results;
INSERT INTO nextgen_v56_keys SELECT plan_id,tvdb_episode,1 FROM sonarr_episode_selections;
INSERT INTO nextgen_v56_keys SELECT plan_id,tvdb_episode,1 FROM sonarr_episode_candidates;
INSERT INTO nextgen_v56_keys SELECT plan_id,tvdb_episode,1 FROM sonarr_magnet_intents;
INSERT INTO nextgen_v56_keys
 SELECT json_extract(payload_json,'$.plan_id'),json_extract(payload_json,'$.'||k.name),
        state NOT IN ('DONE','CANCELED')
 FROM jobs, (SELECT 'retry_episode' name UNION SELECT 'fallback_episode' UNION SELECT 'exhausted_episode') k
 WHERE kind='DISCOVER_SONARR_BACKFILL' AND json_type(payload_json,'$.'||k.name)='integer';

CREATE TEMP TABLE nextgen_v56_map AS
 SELECT k.plan_id,k.tvdb_episode,min(e.n) AS n,count(DISTINCT e.n) AS c
 FROM (SELECT DISTINCT plan_id,tvdb_episode FROM nextgen_v56_keys) k
 JOIN trial_plans p ON p.plan_id=k.plan_id
 JOIN (SELECT anidb_id,tvdb_series_id,tvdb_season,tvdb_episode,anidb_episode_number AS n
       FROM recognized_episodes
       UNION SELECT anidb_id,tvdb_series_id,tvdb_season,tvdb_episode,anidb_episode_number
       FROM release_verifications WHERE tvdb_order='official') e
  ON e.anidb_id=p.anidb_id AND e.tvdb_series_id=p.tvdb_series_id
 AND e.tvdb_season=p.tvdb_season AND e.tvdb_episode=k.tvdb_episode
 GROUP BY k.plan_id,k.tvdb_episode;
-- Ambiguous evidence, required keys without evidence, and key collisions abort.
INSERT OR ROLLBACK INTO nextgen_v56_guard SELECT count(*)=0 FROM nextgen_v56_map WHERE c<>1;
INSERT OR ROLLBACK INTO nextgen_v56_guard SELECT count(*)=0 FROM nextgen_v56_keys k
 WHERE k.required=1 AND NOT EXISTS (SELECT 1 FROM nextgen_v56_map m
  WHERE m.plan_id=k.plan_id AND m.tvdb_episode=k.tvdb_episode);
INSERT OR ROLLBACK INTO nextgen_v56_guard SELECT count(*)=0 FROM
 (SELECT plan_id,n FROM nextgen_v56_map GROUP BY plan_id,n HAVING count(*)>1);

CREATE TABLE trial_plans_v56 (
 plan_id INTEGER PRIMARY KEY,
 anidb_id INTEGER NOT NULL UNIQUE REFERENCES identified_anime(anidb_id),
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>=0),
 tvdb_order TEXT NOT NULL DEFAULT 'official' CHECK(tvdb_order='official'),
 anchor_verification_id INTEGER NOT NULL REFERENCES release_verifications(verification_id),
 anchor_episode INTEGER NOT NULL CHECK(anchor_episode>0),
 backfill_count INTEGER NOT NULL CHECK(backfill_count BETWEEN 0 AND 1000),
 following_count INTEGER NOT NULL CHECK(following_count BETWEEN 0 AND 1000),
 configuration_revision INTEGER NOT NULL CHECK(configuration_revision>0),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 CHECK(backfill_count>0 OR following_count>0),
 CHECK(following_count=0 OR anchor_episode<=9223372036854775807-following_count+1)
);
INSERT INTO trial_plans_v56 SELECT p.plan_id,p.anidb_id,p.tvdb_series_id,p.tvdb_season,p.tvdb_order,
 p.anchor_verification_id,v.anidb_episode_number,p.backfill_count,p.following_count,
 p.configuration_revision,p.created_at_ms
 FROM trial_plans p JOIN release_verifications v ON v.verification_id=p.anchor_verification_id;

CREATE TABLE sonarr_backfill_results_v56 (
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 anidb_episode_number INTEGER NOT NULL CHECK(anidb_episode_number>0),
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 sonarr_episode_id INTEGER CHECK(sonarr_episode_id>0),
 tvdb_episode_id INTEGER CHECK(tvdb_episode_id>0),
 status TEXT NOT NULL CHECK(status IN ('IN_PROGRESS','NOT_LISTED','IDENTITY_UNKNOWN','IDENTITY_CONFLICT','HAS_FILE','CANDIDATES','NO_RESULTS','NOT_AIRED','MAPPING_MISSING','WAITING_FOR_DUB')),
 candidates INTEGER NOT NULL CHECK(candidates BETWEEN 0 AND 1000),
 approved INTEGER NOT NULL CHECK(approved BETWEEN 0 AND candidates),
 exact INTEGER NOT NULL CHECK(exact BETWEEN 0 AND candidates),
 observed_at_ms INTEGER NOT NULL CHECK(observed_at_ms>=0),
 PRIMARY KEY(plan_id,anidb_episode_number)
);
-- Inner join: unconvertible observations are dropped here.
INSERT INTO sonarr_backfill_results_v56 SELECT r.plan_id,m.n,r.job_id,r.sonarr_episode_id,
 r.tvdb_episode_id,r.status,r.candidates,r.approved,r.exact,r.observed_at_ms
 FROM sonarr_backfill_results r JOIN nextgen_v56_map m
 ON m.plan_id=r.plan_id AND m.tvdb_episode=r.tvdb_episode;

CREATE TABLE sonarr_episode_selections_v56 (
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 anidb_episode_number INTEGER NOT NULL CHECK(anidb_episode_number>0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season>=0),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0),
 tvdb_episode_id INTEGER NOT NULL CHECK(tvdb_episode_id>0),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^0-9a-f]*'),
 selected_at_ms INTEGER NOT NULL CHECK(selected_at_ms>=0),
 PRIMARY KEY(plan_id,anidb_episode_number),
 UNIQUE(plan_id,info_hash)
);
-- v55 plans covered exactly one TVDB season, so each selection's placement
-- is (plan season, old episode).
INSERT INTO sonarr_episode_selections_v56 SELECT s.plan_id,m.n,p.tvdb_season,s.tvdb_episode,
 s.tvdb_episode_id,s.info_hash,s.selected_at_ms
 FROM sonarr_episode_selections s JOIN trial_plans p ON p.plan_id=s.plan_id
 JOIN nextgen_v56_map m ON m.plan_id=s.plan_id AND m.tvdb_episode=s.tvdb_episode;

CREATE TABLE sonarr_episode_candidates_v56 (
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 anidb_episode_number INTEGER NOT NULL CHECK(anidb_episode_number>0),
 rank INTEGER NOT NULL CHECK(rank>0 AND rank<=1000),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^0-9a-f]*'),
 state TEXT NOT NULL DEFAULT 'READY' CHECK(state IN ('READY','ACTIVE','FAILED','SUCCEEDED')),
 reason_code TEXT,
 PRIMARY KEY(plan_id,anidb_episode_number,rank),
 UNIQUE(plan_id,anidb_episode_number,info_hash)
);
INSERT INTO sonarr_episode_candidates_v56 SELECT c.plan_id,m.n,c.rank,c.info_hash,c.state,c.reason_code
 FROM sonarr_episode_candidates c JOIN nextgen_v56_map m
 ON m.plan_id=c.plan_id AND m.tvdb_episode=c.tvdb_episode;

CREATE TABLE sonarr_magnet_intents_v56 (
 event_id INTEGER PRIMARY KEY REFERENCES incoming_events(event_id),
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 anidb_episode_number INTEGER NOT NULL CHECK(anidb_episode_number>0),
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
INSERT INTO sonarr_magnet_intents_v56 SELECT i.event_id,i.job_id,i.plan_id,m.n,i.info_hash,i.marker,
 i.api_major,i.base_url,i.save_path,i.category,i.timeout_seconds,i.inventory_json,i.state,
 i.created_at_ms,i.updated_at_ms
 FROM sonarr_magnet_intents i JOIN nextgen_v56_map m
 ON m.plan_id=i.plan_id AND m.tvdb_episode=i.tvdb_episode;

-- Nothing other than observations may be lost.
INSERT OR ROLLBACK INTO nextgen_v56_guard SELECT
 (SELECT count(*) FROM sonarr_episode_selections_v56)=(SELECT count(*) FROM sonarr_episode_selections)
 AND (SELECT count(*) FROM sonarr_episode_candidates_v56)=(SELECT count(*) FROM sonarr_episode_candidates)
 AND (SELECT count(*) FROM sonarr_magnet_intents_v56)=(SELECT count(*) FROM sonarr_magnet_intents)
 AND (SELECT count(*) FROM trial_plans_v56)=(SELECT count(*) FROM trial_plans);

DROP TABLE sonarr_magnet_intents;
DROP TABLE sonarr_episode_candidates;
DROP TABLE sonarr_episode_selections;
DROP TABLE sonarr_backfill_results;
DROP TABLE trial_plans;
ALTER TABLE trial_plans_v56 RENAME TO trial_plans;
ALTER TABLE sonarr_backfill_results_v56 RENAME TO sonarr_backfill_results;
ALTER TABLE sonarr_episode_selections_v56 RENAME TO sonarr_episode_selections;
ALTER TABLE sonarr_episode_candidates_v56 RENAME TO sonarr_episode_candidates;
ALTER TABLE sonarr_magnet_intents_v56 RENAME TO sonarr_magnet_intents;
CREATE UNIQUE INDEX idx_magnet_active_hash ON sonarr_magnet_intents(info_hash) WHERE state<>'RELEASED';

-- Discovery job payload episode fields become AniDB numbers where evidence exists.
UPDATE jobs SET payload_json=json_set(payload_json,'$.'||k.name,(SELECT m.n FROM nextgen_v56_map m
  WHERE m.plan_id=json_extract(jobs.payload_json,'$.plan_id')
  AND m.tvdb_episode=json_extract(jobs.payload_json,'$.'||k.name)))
 FROM (SELECT 'retry_episode' name UNION SELECT 'fallback_episode' UNION SELECT 'exhausted_episode') k
 WHERE jobs.kind='DISCOVER_SONARR_BACKFILL' AND json_type(jobs.payload_json,'$.'||k.name)='integer'
 AND EXISTS (SELECT 1 FROM nextgen_v56_map m
  WHERE m.plan_id=json_extract(jobs.payload_json,'$.plan_id')
  AND m.tvdb_episode=json_extract(jobs.payload_json,'$.'||k.name));

DROP TABLE nextgen_v56_map;
DROP TABLE nextgen_v56_keys;
INSERT OR ROLLBACK INTO nextgen_v56_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v56_guard;
PRAGMA user_version=56;
COMMIT;
PRAGMA foreign_keys=ON;
