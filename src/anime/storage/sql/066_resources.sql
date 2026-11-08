-- v65 -> v66: one record of every file, folder and torrent a job creates
-- (job design JD-1c). The `resources` interface writes these as the work
-- happens; later phases clean up from them alone.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v66_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v66_guard SELECT user_version=65 FROM pragma_user_version;
CREATE TABLE resources (
 resource_id INTEGER PRIMARY KEY,
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 kind TEXT NOT NULL CHECK(kind IN ('TORRENT','DOWNLOADED_FILE','STAGED_FILE','FOLDER','LIBRARY_FILE')),
 -- A torrent is its info hash; everything else is an absolute path.
 info_hash TEXT CHECK(info_hash IS NULL OR (length(info_hash)=40 AND info_hash NOT GLOB '*[^0-9a-f]*')),
 path TEXT CHECK(path IS NULL OR (substr(path,1,1)='/' AND length(path) BETWEEN 2 AND 4096)),
 size INTEGER CHECK(size IS NULL OR size >= 0),
 -- What it came from: a downloaded file's torrent, a staged file's download.
 parent_id INTEGER REFERENCES resources(resource_id),
 state TEXT NOT NULL CHECK(state IN ('PRESENT','DELETED','HANDED_OVER')),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= 0),
 CHECK((kind='TORRENT') = (info_hash IS NOT NULL)),
 CHECK((kind='TORRENT') OR path IS NOT NULL)
);
CREATE UNIQUE INDEX resources_one_torrent_per_job ON resources(job_id, info_hash) WHERE kind='TORRENT';
CREATE UNIQUE INDEX resources_one_path_per_job ON resources(job_id, kind, path) WHERE path IS NOT NULL;
CREATE INDEX resources_by_hash ON resources(info_hash) WHERE info_hash IS NOT NULL;
CREATE INDEX resources_by_path ON resources(path) WHERE path IS NOT NULL;
CREATE TABLE resource_operations (
 operation_id INTEGER PRIMARY KEY,
 resource_id INTEGER NOT NULL REFERENCES resources(resource_id),
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 operation TEXT NOT NULL CHECK(operation IN ('ADDED','DOWNLOADED','STAGED','IMPORTED','DELETED','HANDED_OVER')),
 occurred_at_ms INTEGER NOT NULL CHECK(occurred_at_ms >= 0)
);
CREATE INDEX resource_operations_by_resource ON resource_operations(resource_id, operation_id);
DROP TABLE nextgen_v66_guard;
PRAGMA user_version=66;
COMMIT;
