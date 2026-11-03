# Anime Discovery NextGen — Validation Barrier Register

Prepared: 2026-11-03  
Branch: `fix/validation-simplification`  
Reference: `ANTIGRAVITY_NEXTGEN_FULL_WORK_LIST.md`, `nextgen_full_validation_audit_2026-11-03.md`

## Summary of Completed Remediation Batches

| Batch | Work Items | Focus Area | Status | Key Changes |
|---|---|---|---|---|
| **Batch A** | W-01, W-02, W-03 | Direct Contradictions in Batch Keep & Cleanup | **DONE** | Aligned `BatchKeepJournal.load` signature (`*, resolver=None`); accepted `hash_checked=False` for COPY media receipts. |
| **Batch B** | W-04, W-12 | Identity & Matching Result Contracts | **DONE** | Removed consumer blanket rejection of `attributes`; changed recognized-season cache misses from `failed` to `skipped`. |
| **Batch C** | W-05, W-06 | Keep Data Integrity & Late TVDB Resolution | **DONE** | Scoped Keep episode updates by `plan_id` (preventing cross-anime corruption); replaced catch-all `None` with explicit `KeepUnavailable` / `KeepConfiguration` / `KeepReview`. |
| **Batch D** | W-07, W-08, W-09, W-10, W-11 | Filesystem Trust & Video Handling | **DONE** | Removed `st_dev`/`st_ino`/`ctime` historical equality in Keep; eliminated full video rehashing on mtime touch in Reject cleanup; documented single probe policy. |
| **Batch E** | W-13, W-14, W-15, W-16, W-27 | AniDB-First, Parser & Provider Titles | **DONE** | Eliminated title veto when provider remote IDs link the work; cleaned unbalanced brackets and opaque series IDs; split candidate diagnostics; optional TVDB for AniDB releases; routed batches. |
| **Batch F** | W-17, W-18, W-19, W-20, W-21 | Backfill & Acquisition Discovery | **DONE** | Frozen `plan_window` authority preserved through selection; automatic candidate fallback on failure/timeout. |
| **Batch G** | W-22, W-23, W-24, W-25 | Actionable Diagnostics & Catch Boundaries | **DONE** | Exact catch-boundary logging in worker `_invoke`; structured `failed_check` in Keep review outcomes. |

---

## Actionable Check Register

| File : Line | Predicate / Check | Owner Stage | Input Authority | Action | Stop State | Justification / Protection | Regression Tests |
|---|---|---|---|---|---|---|---|
| `storage/batch_keep_imports.py:95` | `or checked[0]['hash_checked'] is not True` | Batch Keep | Stage publication | **REMOVE** | `NEEDS_REVIEW` | Stage deliberately outputs `hash_checked=False` in COPY mode. Redundant re-check broke valid imports. | `test_batch_keep_service_integration.py` |
| `keep_cleanup_proof.py:32` | `or media[0].get('hash_checked') is not True` | Post-Keep Cleanup | Keep journal | **REMOVE** | `NEEDS_REVIEW` | Copy-mode cleanup repeated the contradictory receipt requirement. | `test_keep_cleanup_proof.py` |
| `jobs/identify_anime_job.py:27` | `if candidate.get("attributes", {}): raise ValueError(...)` | Identify Anime | AniDB resolver | **REMOVE** | `NEEDS_REVIEW` | Denied valid `AnimeSeries` schema fields (`romaji_title`). Schema validator is authoritative. | `test_identify_anime_job.py` |
| `providers/identity.py:169` | `check(stage, "failed", "not_previously_recognized")` | Provider Identity | Local DB cache | **RECLASSIFY** | `NEEDS_REVIEW` | Cache miss was recorded as a failure in trace. Reclassified as normal `skipped` step. | `test_identify_anime_job.py` |
| `keep_resolution.py:82` | `WHERE anidb_episode_number=? AND tvdb_episode_id IS NULL` | Keep Resolution | Staged plan | **CONSOLIDATE** | Silent Data Corruption | Lacked `WHERE plan_id=?`, overwriting other anime sharing the same episode number. Scoped to plan. | `test_keep_resolution.py` |
| `keep_resolution.py:112` | `except Exception: return None` | Keep Resolution | Provider API | **RECLASSIFY** | `NEEDS_REVIEW` | Masked network timeouts, credentials, and SQL conflicts as missing mappings. Replaced with specific exceptions. | `test_keep_resolution.py` |
| `storage/keep_imports.py:67` | `info[:2] != data['source_file'][:2]` (`st_dev`, `st_ino`) | Keep Import | Staged video | **REMOVE** | `NEEDS_REVIEW` | Broken by NFS remounts. Kept regular file and expected size checks only. | `test_keep_filesystem_trust.py` |
| `storage/review_decisions.py:61` | `if info[3] != entry['file'][3]: hash video` | Reject Cleanup | Staged video | **REMOVE** | `NEEDS_REVIEW` | Rehashed multi-gigabyte video on 1ns mtime difference. Verified file size and regular mode instead. | `test_keep_filesystem_trust.py` |
| `providers/identity.py:268` | `not {titles} & {tvdb_titles}` | Provider Identity | TVDB / AniDB | **REMOVE** | `NEEDS_REVIEW` | Title veto rejected legitimate sequels/translations when remote IDs already proved identity link. | `test_sequel_discovery.py` |
| `providers/releases.py:42` | `if type(secret) is not Credentials: raise ProviderAuthenticationFailed()` | Release Verifier | TVDB API | **RECLASSIFY** | `NEEDS_REVIEW` | Blocked AniDB-origin episodes when TVDB credentials absent. Made optional for AniDB origin. | `test_release_verification.py` |
| `storage/sonarr_selection.py:52` | `episode not in Window(...).anidb_episodes(anchor)` | Sonarr Selection | Trial plan | **CONSOLIDATE** | `NEEDS_REVIEW` | Ignored frozen historical window from `sonarr_backfill_results`. Reused authoritative `plan_window`. | `test_backfill_selection.py` |
| `keep_service.py:41-80` | Bare `raise KeepReview()` across 12 sites | Keep Service | Sonarr Client | **CONSOLIDATE** | `NEEDS_REVIEW` | Collapsed all Keep failures to generic reason. Added location-qualified `failed_check` diagnostics. | `test_keep_sonarr.py` |
| `worker.py:166` | `except Exception: return Outcome('retry', 'JOB_HANDLER_FAILED')` | Worker Invocation | Python runtime | **RECLASSIFY** | `RETRY_EXHAUSTED` | Swallowed traceback. Added catch-boundary redacted exception logging with `job_id`/`attempt`. | `test_async_worker.py` |
| `staging.py:109` | `destination(...)` path traversal / symlink check | Staging Publisher | Staging root | **KEEP** | `NEEDS_REVIEW` | Security boundary: prevents writing or unlinking outside configured library/trial roots. | `test_staging_publication.py` |
| `episode_reservations.py:40` | Active reservation exclusivity check | Episode Permit | Database lock | **KEEP** | `ON_HOLD` | Concurrency boundary: ensures only one worker acquires an episode at a time. | `test_episode_reservations.py` |
| `keep_service.py:48` | `not path.is_relative_to(options.library_root)` | Keep Service | Sonarr series | **KEEP** | `NEEDS_REVIEW` | Path containment: prevents importing media into arbitrary system paths outside library root. | `test_keep_sonarr.py` |
