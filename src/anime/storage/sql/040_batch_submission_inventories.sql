-- v39 -> v40: immutable complete source manifest for cancellation recovery.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v40_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v40_guard SELECT user_version=39 FROM pragma_user_version;
CREATE TABLE batch_submission_inventories (
 plan_id INTEGER PRIMARY KEY REFERENCES batch_torrent_submissions(plan_id),
 inventory_json TEXT NOT NULL CHECK(json_valid(inventory_json) AND length(CAST(inventory_json AS BLOB)) BETWEEN 1 AND 1048576),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0)
);
INSERT OR ROLLBACK INTO nextgen_v40_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v40_guard;
PRAGMA user_version=40;
COMMIT;
