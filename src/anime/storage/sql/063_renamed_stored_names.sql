-- v62 -> v63: rename Sonarr-era and "trial" names (refactor RF-006a).
-- Discovery searches Prowlarr, not Sonarr, and "trial" is now "user
-- evaluation". Every row is rewritten, history included, so only the new
-- names exist afterwards. ALTER TABLE ... RENAME also updates the foreign
-- keys and indexes that refer to a renamed table or column.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v63_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v63_guard SELECT user_version=62 FROM pragma_user_version;

ALTER TABLE sonarr_backfill_results RENAME TO episode_discovery_results;
ALTER TABLE sonarr_episode_candidates RENAME TO episode_release_candidates;
ALTER TABLE sonarr_episode_selections RENAME TO episode_release_selections;
ALTER TABLE sonarr_magnet_intents RENAME TO magnet_intents;
ALTER TABLE trial_plans RENAME TO user_evaluation_plans;
ALTER TABLE trial_evaluations RENAME TO user_evaluation_checks;
ALTER TABLE batch_trial_plans RENAME TO batch_user_evaluation_plans;
ALTER TABLE batch_trial_files RENAME TO batch_user_evaluation_files;
ALTER TABLE acquisition_budgets RENAME COLUMN trial_plan_id TO user_evaluation_plan_id;

DROP INDEX idx_trial_evaluations_plan;
CREATE INDEX idx_user_evaluation_checks_plan ON user_evaluation_checks(plan_id);
DROP INDEX idx_batch_trial_by_episode;
CREATE INDEX idx_batch_user_evaluation_by_episode ON batch_user_evaluation_files(plan_id,tvdb_episode);

UPDATE jobs SET kind=CASE kind
  WHEN 'DISCOVER_SONARR_BACKFILL' THEN 'DISCOVER_EPISODE_RELEASES'
  WHEN 'EVALUATE_TRIAL' THEN 'SELECT_USER_EVALUATION'
  WHEN 'STAGE_TRIAL_MEDIA' THEN 'STAGE_USER_EVALUATION_MEDIA' END
 WHERE kind IN ('DISCOVER_SONARR_BACKFILL','EVALUATE_TRIAL','STAGE_TRIAL_MEDIA');
UPDATE job_history SET handler_code=CASE handler_code
  WHEN 'DISCOVER_SONARR_BACKFILL' THEN 'DISCOVER_EPISODE_RELEASES'
  WHEN 'EVALUATE_TRIAL' THEN 'SELECT_USER_EVALUATION'
  WHEN 'STAGE_TRIAL_MEDIA' THEN 'STAGE_USER_EVALUATION_MEDIA' END
 WHERE handler_code IN ('DISCOVER_SONARR_BACKFILL','EVALUATE_TRIAL','STAGE_TRIAL_MEDIA');
-- Internal deliveries created by NextGen discovery; raw_body is unchanged.
UPDATE incoming_events SET source='release-discovery' WHERE source='sonarr-discovery';

DROP TABLE nextgen_v63_guard;
PRAGMA user_version=63;
COMMIT;
