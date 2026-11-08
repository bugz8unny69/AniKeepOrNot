-- v49 -> v50: persist operator-provided series rejection explanations.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v50_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v50_guard SELECT user_version=49 FROM pragma_user_version;
ALTER TABLE anime_review_decisions ADD COLUMN reason_text TEXT
 CHECK(reason_text IS NULL OR (length(trim(reason_text)) BETWEEN 1 AND 500));
DROP TABLE nextgen_v50_guard;
PRAGMA user_version=50;
COMMIT;
