-- v13 -> v14: atomically budget prepared selected videos; preserve prior plans.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v14_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v14_guard SELECT user_version=13 FROM pragma_user_version;
ALTER TABLE acquisition_plans ADD COLUMN selected_size_bytes INTEGER
 CHECK(selected_size_bytes IS NULL OR selected_size_bytes>0);
CREATE TABLE acquisition_budgets (
 trial_plan_id INTEGER PRIMARY KEY REFERENCES trial_plans(plan_id),
 max_episode_bytes INTEGER NOT NULL CHECK(max_episode_bytes BETWEEN 1000000 AND 1000000000000 AND max_episode_bytes%1000000=0),
 max_plan_bytes INTEGER NOT NULL CHECK(max_plan_bytes BETWEEN 1000000 AND 1000000000000 AND max_plan_bytes%1000000=0),
 configuration_revision INTEGER NOT NULL CHECK(configuration_revision>0),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0)
);
DROP TABLE nextgen_v14_guard;
PRAGMA user_version=14;
COMMIT;
