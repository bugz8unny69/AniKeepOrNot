-- v68 -> v69: a series decision can also be ACCEPT (Keep hands the series to
-- Sonarr), and the one Sonarr missing-episode search after Keep is recorded
-- before it is sent (sonarr_search_sent_at_ms), so it is never sent twice.
-- The CHECK names the decisions, so the table is rebuilt. Existing REJECT rows are kept.
BEGIN IMMEDIATE;
CREATE TEMP TABLE anime_v69_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO anime_v69_guard SELECT user_version=68 FROM pragma_user_version;
CREATE TABLE anime_review_decisions_v69 (
 anidb_id INTEGER PRIMARY KEY REFERENCES identified_anime(anidb_id),
 operation_id INTEGER NOT NULL REFERENCES job_operations(operation_id),
 job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(job_id),
 decision TEXT NOT NULL CHECK(decision IN ('REJECT','ACCEPT')),
 cleanup_state TEXT NOT NULL CHECK(cleanup_state IN ('PENDING','COMPLETE')),
 decided_at_ms INTEGER NOT NULL CHECK(decided_at_ms>=0),
 reason_text TEXT CHECK(reason_text IS NULL OR
   (length(trim(reason_text)) BETWEEN 1 AND 500)),
 sonarr_search_sent_at_ms INTEGER CHECK(sonarr_search_sent_at_ms IS NULL OR
   (decision='ACCEPT' AND sonarr_search_sent_at_ms>=0))
);
INSERT INTO anime_review_decisions_v69(anidb_id,operation_id,job_id,decision,cleanup_state,decided_at_ms,reason_text)
SELECT anidb_id,operation_id,job_id,decision,cleanup_state,decided_at_ms,reason_text FROM anime_review_decisions;
DROP TABLE anime_review_decisions;
ALTER TABLE anime_review_decisions_v69 RENAME TO anime_review_decisions;
DROP TABLE anime_v69_guard;
PRAGMA user_version=69;
COMMIT;
