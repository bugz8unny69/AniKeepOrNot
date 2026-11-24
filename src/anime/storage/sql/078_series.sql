-- v77 -> v78: the series job table.
-- One row per AniDB ID, holding its torrents, episodes, and recent history.
BEGIN IMMEDIATE;
CREATE TEMP TABLE anime_v78_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO anime_v78_guard SELECT user_version=77 FROM pragma_user_version;
CREATE TABLE series (
 anidb_id INTEGER PRIMARY KEY,
 title TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('WORKING','ASLEEP','KEPT','REJECTED')),
 wake_at_ms INTEGER,
 torrents_at_once INTEGER NOT NULL DEFAULT 1,
 torrents_json TEXT NOT NULL,
 episodes_json TEXT NOT NULL,
 recent_json TEXT NOT NULL,
 lease_token TEXT,
 lease_expires_at_ms INTEGER,
 created_at_ms INTEGER NOT NULL,
 updated_at_ms INTEGER NOT NULL
);
CREATE INDEX idx_series_state_wake ON series(state, wake_at_ms);
DROP TABLE anime_v78_guard;
PRAGMA user_version=78;
COMMIT;
