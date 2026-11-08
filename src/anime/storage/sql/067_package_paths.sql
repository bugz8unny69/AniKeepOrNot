-- v66 -> v67: the package moved from src/nextgen to src/anime (owner decision, 2026-11-08).
-- Failed checks and other diagnostics stored as "src/nextgen/<file>.py:<line> ..."
-- now say src/anime, so reports can still show the code of old failures.
-- Rewrites every JSON column; generated from the v66 schema.
BEGIN IMMEDIATE;
CREATE TEMP TABLE anime_v67_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO anime_v67_guard SELECT user_version=66 FROM pragma_user_version;
UPDATE acquisition_plans SET manifest_json=replace(manifest_json,'src/nextgen/','src/anime/') WHERE manifest_json IS NOT NULL AND instr(manifest_json,'src/nextgen/')>0;
UPDATE batch_cancellation_cleanup SET files_json=replace(files_json,'src/nextgen/','src/anime/') WHERE files_json IS NOT NULL AND instr(files_json,'src/nextgen/')>0;
UPDATE batch_candidates SET metadata_json=replace(metadata_json,'src/nextgen/','src/anime/') WHERE metadata_json IS NOT NULL AND instr(metadata_json,'src/nextgen/')>0;
UPDATE batch_download_cleanup SET files_json=replace(files_json,'src/nextgen/','src/anime/') WHERE files_json IS NOT NULL AND instr(files_json,'src/nextgen/')>0;
UPDATE batch_episode_media SET evidence_json=replace(evidence_json,'src/nextgen/','src/anime/') WHERE evidence_json IS NOT NULL AND instr(evidence_json,'src/nextgen/')>0;
UPDATE batch_keep_imports SET data_json=replace(data_json,'src/nextgen/','src/anime/') WHERE data_json IS NOT NULL AND instr(data_json,'src/nextgen/')>0;
UPDATE batch_metadata_admissions SET inventory_json=replace(inventory_json,'src/nextgen/','src/anime/') WHERE inventory_json IS NOT NULL AND instr(inventory_json,'src/nextgen/')>0;
UPDATE batch_probe_media SET evidence_json=replace(evidence_json,'src/nextgen/','src/anime/') WHERE evidence_json IS NOT NULL AND instr(evidence_json,'src/nextgen/')>0;
UPDATE batch_staging_publications SET manifest_json=replace(manifest_json,'src/nextgen/','src/anime/') WHERE manifest_json IS NOT NULL AND instr(manifest_json,'src/nextgen/')>0;
UPDATE batch_staging_publications SET receipts_json=replace(receipts_json,'src/nextgen/','src/anime/') WHERE receipts_json IS NOT NULL AND instr(receipts_json,'src/nextgen/')>0;
UPDATE batch_submission_inventories SET inventory_json=replace(inventory_json,'src/nextgen/','src/anime/') WHERE inventory_json IS NOT NULL AND instr(inventory_json,'src/nextgen/')>0;
UPDATE batch_torrent_unlocks SET priorities_json=replace(priorities_json,'src/nextgen/','src/anime/') WHERE priorities_json IS NOT NULL AND instr(priorities_json,'src/nextgen/')>0;
UPDATE cancellation_cleanup SET files_json=replace(files_json,'src/nextgen/','src/anime/') WHERE files_json IS NOT NULL AND instr(files_json,'src/nextgen/')>0;
UPDATE event_history SET details_json=replace(details_json,'src/nextgen/','src/anime/') WHERE details_json IS NOT NULL AND instr(details_json,'src/nextgen/')>0;
UPDATE identity_candidate_evidence SET evidence_json=replace(evidence_json,'src/nextgen/','src/anime/') WHERE evidence_json IS NOT NULL AND instr(evidence_json,'src/nextgen/')>0;
UPDATE identity_results SET series_json=replace(series_json,'src/nextgen/','src/anime/') WHERE series_json IS NOT NULL AND instr(series_json,'src/nextgen/')>0;
UPDATE job_history SET details_json=replace(details_json,'src/nextgen/','src/anime/') WHERE details_json IS NOT NULL AND instr(details_json,'src/nextgen/')>0;
UPDATE jobs SET payload_json=replace(payload_json,'src/nextgen/','src/anime/') WHERE payload_json IS NOT NULL AND instr(payload_json,'src/nextgen/')>0;
UPDATE keep_imports SET data_json=replace(data_json,'src/nextgen/','src/anime/') WHERE data_json IS NOT NULL AND instr(data_json,'src/nextgen/')>0;
UPDATE magnet_intents SET inventory_json=replace(inventory_json,'src/nextgen/','src/anime/') WHERE inventory_json IS NOT NULL AND instr(inventory_json,'src/nextgen/')>0;
UPDATE media_verifications SET evidence_json=replace(evidence_json,'src/nextgen/','src/anime/') WHERE evidence_json IS NOT NULL AND instr(evidence_json,'src/nextgen/')>0;
UPDATE prowlarr_search_snapshots SET snapshot_json=replace(snapshot_json,'src/nextgen/','src/anime/') WHERE snapshot_json IS NOT NULL AND instr(snapshot_json,'src/nextgen/')>0;
UPDATE staging_publications SET manifest_json=replace(manifest_json,'src/nextgen/','src/anime/') WHERE manifest_json IS NOT NULL AND instr(manifest_json,'src/nextgen/')>0;
UPDATE staging_publications SET receipts_json=replace(receipts_json,'src/nextgen/','src/anime/') WHERE receipts_json IS NOT NULL AND instr(receipts_json,'src/nextgen/')>0;
DROP TABLE anime_v67_guard;
PRAGMA user_version=67;
COMMIT;
