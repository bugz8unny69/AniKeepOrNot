-- v58 -> v59: durable candidate evidence for explainable identity matching.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v59_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v59_guard SELECT user_version=58 FROM pragma_user_version;
CREATE TABLE identity_candidate_evidence (
 evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 attempt INTEGER NOT NULL CHECK(attempt >= 0),
 captured_at_ms INTEGER NOT NULL CHECK(captured_at_ms >= 0),
 anidb_id INTEGER NOT NULL CHECK(anidb_id > 0),
 display_title TEXT NOT NULL,
 matched_title TEXT NOT NULL,
 matched_variant TEXT NOT NULL,
 similarity_pct INTEGER NOT NULL CHECK(similarity_pct >= 0 AND similarity_pct <= 100),
 rank INTEGER NOT NULL CHECK(rank >= 1),
 eligible INTEGER NOT NULL CHECK(eligible IN (0, 1)),
 selected INTEGER NOT NULL CHECK(selected IN (0, 1)),
 evidence_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_candidate_evidence_job ON identity_candidate_evidence(job_id, attempt, rank);
CREATE INDEX idx_candidate_evidence_anidb ON identity_candidate_evidence(anidb_id);
INSERT OR ROLLBACK INTO nextgen_v59_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v59_guard;
PRAGMA user_version=59;
COMMIT;
