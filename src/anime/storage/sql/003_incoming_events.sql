-- NextGen schema v3. Preserve historical v1/v2 scripts unmodified.
-- Apply only when PRAGMA user_version=2 in a controlled migration runner.
-- SQLite updates the references from event_history, identity_results and jobs.
BEGIN IMMEDIATE;
ALTER TABLE webhook_events RENAME TO incoming_events;
PRAGMA user_version=3;
COMMIT;
