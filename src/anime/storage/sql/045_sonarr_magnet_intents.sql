-- v44 -> v45: pre-manifest one-shot Sonarr magnet intent; never stores magnets.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v45_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v45_guard SELECT user_version=44 FROM pragma_user_version;
CREATE TABLE sonarr_magnet_intents (
 event_id INTEGER PRIMARY KEY REFERENCES incoming_events(event_id),
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode BETWEEN 1 AND 1000),
 info_hash TEXT NOT NULL UNIQUE CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^0-9a-f]*'),
 marker TEXT NOT NULL UNIQUE CHECK(length(marker)=32 AND marker NOT GLOB '*[^0-9a-f]*'),
 api_major INTEGER NOT NULL CHECK(api_major IN (4,5)),
 base_url TEXT NOT NULL,
 save_path TEXT NOT NULL,
 category TEXT NOT NULL,
 timeout_seconds INTEGER NOT NULL CHECK(timeout_seconds BETWEEN 1 AND 60),
 inventory_json TEXT CHECK(inventory_json IS NULL OR json_valid(inventory_json)),
 state TEXT NOT NULL DEFAULT 'UNCERTAIN' CHECK(state IN ('UNCERTAIN','OWNED_STOPPED','REVIEW','EXTERNAL')),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=0),
 UNIQUE(plan_id,tvdb_episode,info_hash)
);
INSERT OR ROLLBACK INTO nextgen_v45_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v45_guard;
PRAGMA user_version=45;
COMMIT;
