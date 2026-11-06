# Audit of the NextGen work merged at `1c785beb`

**Scope:** the code that the `1c785beb` sync brought into `refactor`: the batch candidate controller stack (#274–#278) and the installer changes (#280). It was reviewed against the refactor's purpose: dead code, duplication, naming, reason codes, and anything that later refactor items must account for. NextGen stays the source of truth, so nothing here changes behaviour.

## Changed in this PR

| # | Finding | Change |
|---|---|---|
| A1 | 8 unused imports and 1 unreferenced constant from the new code (listed in `DEAD_CODE_AUDIT.md`, "Added by later NextGen merges") | Removed. Each was confirmed by the RF-002 scanner and by a text search of its file. |
| A2 | Six `sqlite_master` table probes in the batch dashboard views (`get_batch_scope_operation_view`, `list_batch_scopes`) | Removed. Every connection the views get has passed the schema-version check, so the tables always exist; the probes could only hide a broken database by returning nothing. |
| A3 | qBittorrent's four stopped states were defined 4 times as a constant (`transfers`, `sonarr_magnet_intake`, `batch_metadata_admission`, `metadata_service`) and written inline 6 more times (`qbittorrent` ×4, `submission`, `batch/qb_client`) | One definition, `acquisition/metadata_service.STOPPED`. The other modules import it under the same name, so existing `from … import STOPPED` keeps working. |
| A4 | Two parsers of the same stored delivery body: `storage/batch_candidates.scope_releases()` and `batch/backlog_audit._release()` | One parser, `batch_candidates.release_facts()`, used by both. |
| A5 | Two different classes named `CandidateConflict` (`storage/sonarr_candidates.py`, `storage/batch_candidates.py`) | The batch one is now `BatchCandidateConflict`. |

The test count is unchanged (1,678 tests; 35/35 batches). No test was weakened.

## For later refactor items

| # | Finding | Proposed handling |
|---|---|---|
| L1 | **Torrent ownership rule written out ~11 times.** "The torrent carries exactly our `ad-<marker>` tag and no other `ad-` tag, in the expected folder and category, with automatic management off" appears inline in `transfers`, `submission`, `cleanup`, `qbittorrent` (×3), `batch/transfer`, `batch/probe_transfer`, `batch/qb_client` and `batch_submission_journal`. #275 added the shared `metadata_service.verify_torrent_ownership()`, but only the metadata paths use it. | A separate work item. The copies differ in small ways: some also check the hash, a stopped state, a non-empty file list, or strip a trailing `/` from the path. Each must keep its exact rule. That makes this a careful, call-site-by-call-site change to safety checks, not a mechanical one. |
| L2 | **New tables store reason codes.** `batch_candidates.reason_code` and `batch_candidate_stops.reason_code` keep the reason a candidate stopped (for example `TORRENT_METADATA_TIMEOUT`). | RF-006's rename migration must rewrite these two columns too, along with job history and job rows. |
| L3 | **Migration numbers.** The stack's 060 (`batch_candidate_operations`) and 061 (`batch_candidate_stops`) collide with 060 (`service_status`) on `services`. | Renumber the `services` migration to 062 when `services` is merged into `refactor` (already planned). |
| L4 | No new "trial" naming in the new code. The older `batch_trial_plans` tables and `trial_*` names it uses are already covered by RF-005 (rename to user evaluation). | Nothing extra. |

## Belongs on NextGen (behaviour)

| # | Finding | Effect today |
|---|---|---|
| N1 | When a failing batch job has no reason recorded, the candidate's stop falls back to `'JOB_FAILED'` (`batch_candidates.auto_cleanup_failed_batch_candidate`, `_legacy_disposition`). That isn't a registered reason code. | Rare: a failing job normally has a reason. The code would show as an unknown reason in candidate views. Fix on NextGen by using the registered `JOB_HANDLER_FAILED`. |
| N2 | `BatchCandidateConflict.reason_code = 'BATCH_SELECTION_CONFLICT'` isn't registered. | Harmless: it never becomes a job's reason, and the worker reports `JOB_HANDLER_FAILED` for unregistered codes. It could be removed or registered on NextGen. |
