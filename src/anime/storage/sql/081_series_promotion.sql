-- v80 -> v81: a promoted series is worked on first (owner, 2026-11-28).
-- Its own column: the worker saves a series it holds without touching it, so a promotion
-- made while the series is being worked on is never lost.
BEGIN IMMEDIATE;
CREATE TEMP TABLE anime_v81_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO anime_v81_guard SELECT user_version=80 FROM pragma_user_version;
ALTER TABLE series ADD COLUMN promoted_at_ms INTEGER;
DROP TABLE anime_v81_guard;
PRAGMA user_version=81;
COMMIT;
