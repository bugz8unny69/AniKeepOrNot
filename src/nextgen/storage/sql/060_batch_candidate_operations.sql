-- v59 -> v60: series-scoped batch candidate controller and candidate pooling.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v60_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v60_guard SELECT user_version=59 FROM pragma_user_version;

CREATE TABLE batch_candidate_scopes (
 scope_id INTEGER PRIMARY KEY,
 anidb_id INTEGER NOT NULL CHECK(anidb_id > 0),
 tvdb_series_id INTEGER NOT NULL CHECK(tvdb_series_id > 0),
 tvdb_season INTEGER NOT NULL CHECK(tvdb_season > 0),
 operation_id INTEGER REFERENCES job_operations(operation_id),
 active_candidate_id INTEGER,
 controller_job_id INTEGER REFERENCES jobs(job_id),
 state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(state IN ('ACTIVE','EXHAUSTED','SUCCEEDED')),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
 UNIQUE(anidb_id, tvdb_season),
 FOREIGN KEY(anidb_id) REFERENCES identified_anime(anidb_id)
);
CREATE INDEX idx_batch_candidate_scopes_operation ON batch_candidate_scopes(operation_id);

CREATE TABLE batch_candidates (
 candidate_id INTEGER PRIMARY KEY,
 scope_id INTEGER NOT NULL REFERENCES batch_candidate_scopes(scope_id),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^0-9a-f]*'),
 title TEXT NOT NULL CHECK(length(trim(title)) > 0),
 rank INTEGER NOT NULL CHECK(rank > 0),
 state TEXT NOT NULL DEFAULT 'READY' CHECK(state IN ('READY','ACTIVE','FAILED','SUCCEEDED','CANCELED')),
 reason_code TEXT,
 has_metadata INTEGER NOT NULL DEFAULT 0 CHECK(has_metadata IN (0, 1)),
 metadata_json TEXT CHECK(metadata_json IS NULL OR json_valid(metadata_json)),
 seeders INTEGER CHECK(seeders IS NULL OR seeders >= 0),
 size_bytes INTEGER CHECK(size_bytes IS NULL OR size_bytes > 0),
 source_event_id INTEGER REFERENCES incoming_events(event_id),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
 UNIQUE(scope_id, info_hash)
);
CREATE INDEX idx_batch_candidates_scope_state ON batch_candidates(scope_id, state, rank);
CREATE INDEX idx_batch_candidates_hash ON batch_candidates(info_hash);

CREATE TABLE batch_candidate_events (
 scope_id INTEGER NOT NULL REFERENCES batch_candidate_scopes(scope_id),
 info_hash TEXT NOT NULL CHECK(length(info_hash)=40 AND info_hash NOT GLOB '*[^0-9a-f]*'),
 event_id INTEGER NOT NULL REFERENCES incoming_events(event_id),
 received_at_ms INTEGER NOT NULL CHECK(received_at_ms >= 0),
 PRIMARY KEY(scope_id, info_hash, event_id)
);
CREATE INDEX idx_batch_candidate_events_event ON batch_candidate_events(event_id);

INSERT OR ROLLBACK INTO nextgen_v60_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v60_guard;
PRAGMA user_version=60;
COMMIT;
