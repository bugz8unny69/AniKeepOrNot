-- v8 -> v9: shared provider pacing and bounded public AniDB/mapping cache.
-- Maintenance only; no credentials, bearer tokens or authenticated responses.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v9_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v9_guard SELECT user_version=8 FROM pragma_user_version;
CREATE TABLE provider_limits (
 provider TEXT PRIMARY KEY CHECK(provider IN ('anidb','anidb_titles','anime_lists','tvdb')),
 next_allowed_at_ms INTEGER NOT NULL CHECK(next_allowed_at_ms >= 0),
 failures INTEGER NOT NULL DEFAULT 0 CHECK(failures BETWEEN 0 AND 16),
 reservation_token TEXT NOT NULL CHECK(length(reservation_token)=32)
);
CREATE TABLE provider_public_cache (
 provider TEXT NOT NULL CHECK(provider IN ('anidb','anidb_titles','anime_lists')),
 cache_key TEXT NOT NULL CHECK(length(cache_key) BETWEEN 1 AND 64),
 fetched_at_ms INTEGER NOT NULL CHECK(fetched_at_ms >= 0),
 expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms > fetched_at_ms),
 payload BLOB NOT NULL CHECK(typeof(payload)='blob' AND length(payload) BETWEEN 1 AND 67108864),
 PRIMARY KEY(provider,cache_key)
);
CREATE INDEX idx_provider_cache_expiry ON provider_public_cache(expires_at_ms);
DROP TABLE nextgen_v9_guard;
PRAGMA user_version=9;
COMMIT;
