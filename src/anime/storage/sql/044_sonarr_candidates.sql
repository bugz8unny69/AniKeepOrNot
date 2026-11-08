-- v43 -> v44: ordered local candidates for restart-safe fallback; no magnets or credentials.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v44_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v44_guard SELECT user_version=43 FROM pragma_user_version;
CREATE TABLE sonarr_episode_candidates (
 plan_id INTEGER NOT NULL REFERENCES trial_plans(plan_id),
 tvdb_episode INTEGER NOT NULL CHECK(tvdb_episode>0 AND tvdb_episode<=1000),
 rank INTEGER NOT NULL CHECK(rank>0 AND rank<=1000),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^0-9a-f]*'),
 state TEXT NOT NULL DEFAULT 'READY' CHECK(state IN ('READY','ACTIVE','FAILED','SUCCEEDED')),
 reason_code TEXT,
 PRIMARY KEY(plan_id,tvdb_episode,rank),
 UNIQUE(plan_id,tvdb_episode,info_hash)
);

INSERT OR ROLLBACK INTO nextgen_v44_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v44_guard;
PRAGMA user_version=44;
COMMIT;
