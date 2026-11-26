-- v77 -> v78: keep only the latest saved result of each Prowlarr search
-- (owner, 2026-11-26). A search is the series, season, episode and mode; every
-- search added a row and none was ever removed (22 MB on 2026-11-26).
BEGIN IMMEDIATE;
CREATE TEMP TABLE anime_v78_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO anime_v78_guard SELECT user_version=77 FROM pragma_user_version;
DELETE FROM prowlarr_search_snapshots WHERE snapshot_id NOT IN (
 SELECT max(snapshot_id) FROM prowlarr_search_snapshots GROUP BY tvdb_series_id,season,episode,search_mode);
CREATE INDEX idx_prowlarr_snapshots_search ON prowlarr_search_snapshots(tvdb_series_id,season,episode,search_mode);
DROP TABLE anime_v78_guard;
PRAGMA user_version=78;
COMMIT;
