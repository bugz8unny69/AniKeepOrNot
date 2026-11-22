-- v76 -> v77: releases waiting for the API's identity process (identity bridge).
-- A row lives only until its release is identified (the API then gives the
-- worker a job) or rejected (API Reject keeps the release in the notification).
BEGIN IMMEDIATE;
CREATE TEMP TABLE anime_v77_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO anime_v77_guard SELECT user_version=76 FROM pragma_user_version;
CREATE TABLE identity_requests (
 request_id INTEGER PRIMARY KEY,
 raw_body BLOB NOT NULL CHECK(length(raw_body) BETWEEN 1 AND 65536),
 process_anyway INTEGER NOT NULL DEFAULT 0 CHECK(process_anyway IN (0,1)),
 received_at_ms INTEGER NOT NULL CHECK(received_at_ms>=0),
 next_try_at_ms INTEGER NOT NULL CHECK(next_try_at_ms>=0),
 tries INTEGER NOT NULL DEFAULT 0 CHECK(tries>=0),
 last_reason_code TEXT
);
CREATE INDEX idx_identity_requests_next ON identity_requests(next_try_at_ms,request_id);
-- The old identification job is gone: the API identifies now. Its unfinished
-- jobs move to the bridge (identified again, or refused with API Reject), and
-- every old identification job whose release was never identified is removed
-- with its delivery, so no row is left that nothing will ever finish.
CREATE TEMP TABLE anime_v77_old_identity AS
SELECT j.job_id,j.event_id,j.state,j.payload_json FROM jobs j JOIN incoming_events e USING(event_id)
WHERE j.kind='IDENTIFY_ANIME' AND j.state<>'DONE' AND e.state<>'IDENTIFIED'
  AND length(e.raw_body) BETWEEN 1 AND 65536
  AND NOT EXISTS (SELECT 1 FROM jobs x WHERE (x.event_id=j.event_id AND x.job_id<>j.job_id) OR x.created_by_job_id=j.job_id)
  AND NOT EXISTS (SELECT 1 FROM identity_results r WHERE r.event_id=j.event_id)
  AND NOT EXISTS (SELECT 1 FROM magnet_intents m WHERE m.event_id=j.event_id OR m.job_id=j.job_id)
  AND NOT EXISTS (SELECT 1 FROM batch_metadata_admissions m WHERE m.event_id=j.event_id)
  AND NOT EXISTS (SELECT 1 FROM manual_release_submissions m WHERE m.event_id=j.event_id)
  AND NOT EXISTS (SELECT 1 FROM manual_release_archive m WHERE m.event_id=j.event_id)
  AND NOT EXISTS (SELECT 1 FROM batch_candidate_events m WHERE m.event_id=j.event_id)
  AND NOT EXISTS (SELECT 1 FROM batch_candidates m WHERE m.source_event_id=j.event_id)
  AND NOT EXISTS (SELECT 1 FROM management_actions m WHERE m.job_id=j.job_id OR m.target_job_id=j.job_id)
  AND NOT EXISTS (SELECT 1 FROM prowlarr_search_snapshots m WHERE m.job_id=j.job_id)
  AND NOT EXISTS (SELECT 1 FROM resources m WHERE m.job_id=j.job_id)
  AND NOT EXISTS (SELECT 1 FROM resource_operations m WHERE m.job_id=j.job_id);
INSERT INTO identity_requests(raw_body,process_anyway,received_at_ms,next_try_at_ms)
SELECT e.raw_body,CASE WHEN json_extract(o.payload_json,'$.process_anyway') IS 1 THEN 1 ELSE 0 END,
       e.received_at_ms,e.received_at_ms
FROM anime_v77_old_identity o JOIN incoming_events e USING(event_id)
WHERE o.state NOT IN ('CANCELED','FAILED') ORDER BY e.event_id;
CREATE TEMP TABLE anime_v77_old_operations AS
SELECT DISTINCT operation_id FROM job_operation_members WHERE job_id IN (SELECT job_id FROM anime_v77_old_identity);
UPDATE notifications SET job_id=NULL WHERE job_id IN (SELECT job_id FROM anime_v77_old_identity);
DELETE FROM identity_candidate_evidence WHERE job_id IN (SELECT job_id FROM anime_v77_old_identity);
DELETE FROM job_history WHERE job_id IN (SELECT job_id FROM anime_v77_old_identity);
DELETE FROM job_operation_members WHERE job_id IN (SELECT job_id FROM anime_v77_old_identity);
DELETE FROM jobs WHERE job_id IN (SELECT job_id FROM anime_v77_old_identity);
DELETE FROM event_history WHERE event_id IN (SELECT event_id FROM anime_v77_old_identity);
DELETE FROM incoming_events WHERE event_id IN (SELECT event_id FROM anime_v77_old_identity);
DELETE FROM job_operations WHERE operation_id IN (SELECT operation_id FROM anime_v77_old_operations)
  AND NOT EXISTS (SELECT 1 FROM job_operation_members m WHERE m.operation_id=job_operations.operation_id)
  AND NOT EXISTS (SELECT 1 FROM cancellation_cleanup m WHERE m.operation_id=job_operations.operation_id)
  AND NOT EXISTS (SELECT 1 FROM staging_cleanup_artifacts m WHERE m.operation_id=job_operations.operation_id)
  AND NOT EXISTS (SELECT 1 FROM management_actions m WHERE m.operation_id=job_operations.operation_id)
  AND NOT EXISTS (SELECT 1 FROM anime_review_decisions m WHERE m.operation_id=job_operations.operation_id)
  AND NOT EXISTS (SELECT 1 FROM batch_download_cleanup m WHERE m.operation_id=job_operations.operation_id)
  AND NOT EXISTS (SELECT 1 FROM batch_cancellation_cleanup m WHERE m.operation_id=job_operations.operation_id)
  AND NOT EXISTS (SELECT 1 FROM batch_candidate_scopes m WHERE m.operation_id=job_operations.operation_id)
  AND NOT EXISTS (SELECT 1 FROM batch_candidate_stops m WHERE m.operation_id=job_operations.operation_id);
DROP TABLE anime_v77_old_operations;
DROP TABLE anime_v77_old_identity;
DROP TABLE anime_v77_guard;
PRAGMA user_version=77;
COMMIT;
