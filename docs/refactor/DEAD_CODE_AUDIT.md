# Dead-code and duplication audit (RF-002)

**Base:** `NextGen` `99dd30ef` (see [BASELINE.md](BASELINE.md))
**Scope:** `src/anime` excluding tests (167 files, 27,589 lines), plus entrypoints `bin/adm`, `bin/anime-discovery-install`, `deploy/nextgen/*.service`, the operator repair scripts in `fixes/`, dashboard JS and SQL.

> **Correction (RF-003):** the first version of this audit did not scan `fixes/`. As a result `audio_exhausted` (formerly D4) was wrongly listed as dead: `fixes/requeue_waiting_for_dub.py` imports it. The full suite caught this when it was removed, and the removal was reverted. Every other removed name and every P-item was then rechecked against `fixes/`, `bin/` and `deploy/`, and no other references were found.
**Production behavior changed:** none. This is an audit only.

## Method

1. **AST definition scan.** Every top-level function, class and constant, and every public method, was checked against word-boundary references in all NextGen Python, dashboard JS/CJS, SQL, `bin/` and `deploy/`. A definition counts as *unreferenced* when its name appears only at the definition. It counts as *test-only* when every other reference is in `src/anime/tests/`.
2. **Module scan.** Every module stem was checked for references outside its own file. **No module is unreferenced or test-only.**
3. **Import scan.** For every imported name not used in its file, the check also looked for indirect use: re-export (`from <module> import name` elsewhere), `module.name` attribute access, and `mock.patch` target strings.
4. **Dynamic contracts.** Every job `KIND` (29) was traced to its `registration()` and its enqueue sites. All entrypoints were checked: `python -m src.anime worker` → `providers/configuration.py` → `serve_worker`, and `python -m src.anime.api serve`. Persisted identifiers in `storage/sql/` were checked, as were dashboard handler names in `dashboard/presentation.py`.
5. **History.** For each candidate, `git log -S` / `-G` found the commit that removed its last caller. That commit is the evidence for the classification.
6. **Duplication.** AST-normalized function bodies were hashed to find exact duplicates, and helper names repeated across modules were listed.

Passing tests were not taken as proof that code is used. Several test-only items below are classified *potentially dead* rather than *confirmed dead* because of that.

Historical migration scripts (`src/anime/storage/sql/`, `storage/migrations.py`) are excluded from deletion.

---

## 1. Confirmed dead (may be removed in RF-003)

| # | Location | Item | Evidence |
|---|---|---|---|
| D1 | `src/anime/sonarr.py:139` | `Client.add_payload()` | Its only caller (`check_source(data);payload=client.add_payload(selection)`) was removed in `b0ca9e37`. Keep now requires the series to exist already and fails with `SERIES_MISSING` (`keep_service.py:45`). No production or test references remain. This was the last piece of the superseded Sonarr "add series" acquisition path. |
| D2 | `src/anime/acquisition/prowlarr_search.py:281` | `_release()` | Its row-filtering logic was inlined into `Client._search()` (`prowlarr_search.py:551`) for the explainable matching report in `45e1876a`. Zero references. |
| D3 | `src/anime/acquisition/audio.py:68` | `audio_tagged()` and its `_TAG` regex, if `_TAG` turns out to be used only there | `dual_audio_tagged()` replaced it in `c9c14c35` ("gate English acquisitions on dual-audio hints"), which removed both production callers. Only its own unit assertions in `tests/test_audio_check.py:28,34` remain. Those assertions go with it. |
| D5 | `src/anime/acquisition/audio.py:11` | `MODES` | Zero references. Configuration validates audio modes on its own. |
| D6 | `src/anime/storage/prowlarr_snapshots.py:12` | `MAX_INDEXER_LEN` | Zero references. Removing an unused constant changes no bounds. |
| D7 | (list below) | Unused imports | Not used in their file, not re-exported, not a patch target. |

### D7 — unused imports

Function-local imports are marked *(local)*. Where removing a name deletes the whole `import` statement, RF-003 must confirm that the imported module has no import-time side effects. The full suite would also catch import-order regressions.

| File:line | Unused name(s) |
|---|---|
| `acquisition/metainfo_source.py:11` | `MetadataInvalid` |
| `acquisition/metainfo_source.py:123,127` *(local)* | `_inventory`, `ObservedBatchInventory` |
| `acquisition/qbittorrent.py:241` *(local)* | `MetadataInvalid` |
| `acquisition/readiness.py:2,7,13` *(added by #261)* | `logging`, `DownloadOptions`, `DownloadCredentials`, `event` |
| `api/__main__.py:9` | `ApiError` |
| `api/admin_socket.py:10` | `PasswordStore` |
| `api/app.py:17` | `bearer`, `matches` |
| `api/app.py:26` | `address_mismatch` |
| `batch/probe_transfer.py:9` | `PieceOverlap` |
| `batch/transfer.py:7,13` | `field`, `PieceOverlap` |
| `batch/unlock.py:11` | `ACTIVE`, `CHECKING`, `STOPPED`, `PieceOverlap` |
| `jobs/reject_series_job.py:15` | `operation` |
| `jobs/submit_batch_job.py:27` | `_target` |
| `jobs/transfer_batch_job.py:25,28,29` | `_record`, `_intent`, `_candidate` |
| `keep_resolution.py:5,8` | `Options`, `ProviderInvalidResponse` |
| `keep_service.py:7` | `KeepUnavailable` |
| `providers/backfill_catalog.py:10,11` | `ProviderAuthenticationFailed`, `Credentials` |
| `staging.py:9,14` | `stat`, `CancellationError` |
| `storage/batch_episode_media.py:6,7` | `integer`, `_decode_media` |
| `storage/batch_episode_source.py:2` | `AcquisitionConflict` |
| `storage/batch_keep_imports.py:9` | `PurePosixPath` |
| `storage/batch_probe_media.py:2,6,8,9,10` | `json`, `selection_fingerprint`, `_candidate`, `_intent`, `_record` |
| `storage/batch_probe_source.py:2` | `AcquisitionConflict` |
| `storage/batch_staging.py:15` | `VerifiedMedia` |
| `storage/batch_transfer_journal.py:9` | `STOPPED` |
| `storage/batch_verifications.py:12` | `BatchPlanningError` |
| `storage/job_queue.py:16` | `payload_json` |
| `storage/keep_imports.py:10` | `signature` |
| `storage/manual_release.py:12` | `re` |
| `storage/review_decisions.py:9` | `signature` |
| `storage/sonarr_selection.py:9` | `_magnet_hash` |
| `worker.py:20` | `event` |

---

## 2. Potentially dead (investigate; not removed in RF-003 without a decision)

These are production definitions referenced **only by tests**. Deleting them would also delete or rewrite tests, and several of them encode invariants that the tests deliberately pin down.

| # | Location | Item | Test refs | Notes |
|---|---|---|---|---|
| P2 | `batch/window.py:27` | `plan_batch_trial_window()` | 7 | The production batch flow plans through `batch/selection.plan_verified_batch` and `storage/sonarr_backfill.plan_window`. It is unclear whether these validated planners are superseded or still planned. |
| P3 | `batch/verification.py:151` | `plan_provider_verified_batch()` | 6 | Same group as P2. |
| P4 | `batch/transfer.py:30` | `plan_stopped_batch_probe()` | 2 | Same group as P2. Its docstring describes the E01-first probe intent. |
| P5 | `storage/batch_acquisition_candidate.py:95` | `load_batch_acquisition_candidate()` | 3 | A read-only inspector. Its tests cover hash-bound inventory checks. |
| P6 | `worker.py:355` | `run_worker()` | 17 | A test convenience wrapper. Production composes the worker in `providers/configuration.py:140` and calls `serve_worker()` directly. Moving it into a test helper would be cleaner, but it is small. |
| P7 | `storage/sqlite.py:870` | `accept_event()` | 20 | The test fixture builder for intake rows. Production intake writes through its own path. It could move into test support. |
| P8 | `storage/sqlite.py:972` | `save_identity()` | 39 | Same as P7, for identities. It wraps `persist_identity` in a transaction. |
| P9 | `storage/sqlite.py:795` | `admitted_connection()` | 3 | Its docstring calls it the pattern for *new* runtime operations, so this may be intended API that nothing has adopted yet. |
| P10 | `settings.py:44` | `load_settings()` | 5 | A logging-only compatibility reader. Production reads configuration through `ConfigurationStore`. |
| P11 | `diagnostics.py:222` | `Span` | 2 | A trivial monotonic timer that only tests use. |
| P12 | `acquisition/settings.py:108` | `download_context()` | 3 | Production calls `download_options()` and `qbittorrent_credentials()` separately. |
| P13 | Multiple storage modules | Read-only snapshot boilerplate | n/a | See §5, duplication. |
| P14 | `acquisition/readiness.py:126` *(added by #261)* | `QbittorrentReadiness.last_error()` | 0 | A public accessor that was added with the new readiness cache but that nothing calls. The code is brand new and the accessor may be intended for diagnostics, so it is left until the owner decides. |


### Added by later NextGen merges (queued for RF-008)

| Location | Item | Class | Notes |
|---|---|---|---|
| `dashboard/assets.py:28` | `address_mismatch()` | Confirmed dead | Its last (unused) import in `api/app.py` was removed in RF-003. No references remain in `src/anime`, `fixes/`, `bin/` or `deploy/`. |
| `providers/scoring.py:13` *(#267)* | `import unicodedata` | Confirmed dead import | |
| `tools/evaluate_historical_identities.py:10,16,404` *(#267)* | `dataclass`, `CandidateMatch`, local `re` | Confirmed dead imports | |
| `providers/parsing.py:22` *(#267)* | `METADATA_ONLY` | Potentially dead | No references. It may be an intended parse-mode constant, so check before removing. |
| `storage/candidate_evidence.py:102` *(#267)* | `count_candidate_evidence()` | Potentially dead | No references, including tests. |
| `tools/evaluate_historical_identities.py` *(#267)* | module | **Active** | A standalone offline evaluation tool (`__main__`), documented in `docs/ANIDB_IDENTITY_MATCHING.md`. It is not imported, which is expected for a tool. |
| `acquisition/legacy_magnet.py:6` *(#275)* | `import asyncio` | Confirmed dead import | Its semaphore moved to `metadata_service.shared_limiter()`. **Removed** in the `1c785beb` audit. |
| `acquisition/metainfo_source.py` *(#275)* | local `import inspect`, local `STOPPED` import | Confirmed dead imports | Polling moved to `metadata_service.poll_or_stop_metadata()`. **Removed** in the `1c785beb` audit. |
| `acquisition/metadata_service.py:18` *(#275)* | `METADATA_TIMEOUT_SECONDS` | Potentially dead | No references; `METADATA_TIMEOUT_MS` is the one used. **Removed** in the `1c785beb` audit. |
| `providers/configuration.py:19` *(#277)* | `KIND as BATCH_PROWLARR_KIND` | Confirmed dead import | Only `batch_prowlarr_registration` from that import is used. **Removed** in the `1c785beb` audit. |
| `storage/batch_candidates.py` *(#274-#278)* | `field`, `sqlite3`, `delivery_title`, `ReleaseInput` imports | Confirmed dead imports | Left over after the controller moved coordination into `coordinate_release()`. **Removed** in the `1c785beb` audit. |

---

## 3. Required compatibility (must remain supported)

| Item | Why it stays |
|---|---|
| Job kind `DISCOVER_SONARR_BACKFILL` | Persisted in `jobs` rows and history. The module docstring says the kind "keeps its historical name so existing jobs and history stay valid". The rename is handled in RF-004 and RF-006, not by deletion. |
| Tables `sonarr_episode_candidates`, `sonarr_episode_selections`, `sonarr_backfill_*` and the `sonarr-discovery` event source | Persisted schema and event data. Referenced from `storage/sql/`, cleanup, retry and recovery code. Covered by RF-004 and RF-006. |
| `storage/sql/*` and `storage/migrations.py` | Historical migrations are excluded from deletion. |
| Obsolete HTTP metadata-source fields in `configuration_schema.py:129,585` | Still needed to validate and upgrade previously published configuration versions. |
| `acquisition/legacy_magnet.py`, `storage/sonarr_magnet_intake.py` | An active hash-bound magnet bootstrap, used for backfill-selected and ordinary webhook magnets in the existing PREPARE job. "Legacy" describes the magnet format, not dead code. |
| `storage/sonarr_cached_retry.py` | The active "Search again" saved-magnet replay path, used by recovery and review retry. |
| `schema/__init__.py` re-exports (`AnimeSeries`, `Anime`, `Episode`, `TVDBPlacement`) | The canonical package surface ("Canonical versioned series schema"). No current importer uses it, but it is the documented public entry. Keep it unless RF-008 decides otherwise. |
| `storage/staging.py:13` `StagingConfiguration` | Re-exported. Other modules import it from here. |
| `storage/prowlarr_snapshots.py:122` `prune_snapshots()` | **Kept by owner decision (2026-11-04). This function is retained but not yet wired up.** It is the retention policy for `prowlarr_search_snapshots`: delete snapshots older than 30 days, and keep the latest 5 per job, but only for `DONE` or `CANCELED` jobs. That table is active: `job_queue.py:231` writes a row for every Prowlarr search, and `adm report --include-matching prowlarr` reads them. Because nothing calls this function, the rows are never pruned. Wiring it in would be a behavior change, so it belongs on NextGen, not in this refactor. Do not remove it. |
| `storage/sonarr_backfill.py` `audio_exhausted()` *(formerly D4)* | Imported by the operator repair script `fixes/requeue_waiting_for_dub.py:29`, which `tests/test_waiting_for_dub.py` loads. It is not dead. |
| `schema/anime_series.py:14` `FIELD_DOC_KEYS` | A schema-documentation contract constant. `test_anime_series_schema.py` uses it to make every field carry documentation metadata. |

---

## 4. Active (retain)

- **All 29 job kinds** are registered (`registration()` in 29 modules) and enqueued, either directly or through `KIND` constants. Three kinds have a single textual reference, but each is used through its `KIND` constant and `enqueue()`:
  - `REFRESH_JELLYFIN`, enqueued from `keep_media_job`, `keep_batch_job`, `stage_batch_episode_job` and `stage_trial_media_job`;
  - `ENRICH_SHOKO_METADATA`, from `keep_media_job` and `jobs/management.py`;
  - `BULK_MANAGE_JOBS`, from `jobs/management.py` and `storage/management_actions.py`.
- **`SafeFormatter.formatException` / `formatStack`** (`diagnostics.py:63,66`) override `logging.Formatter` methods. The standard library calls them, so they are not dead even though nothing calls them by name.
- **Every module** has a production importer (§ Method, step 2).
- **Sonarr-named discovery modules** are all live. They are misnamed, not dead. RF-004 maps them:
  - `jobs/discover_sonarr_backfill_job.py`
  - `storage/sonarr_backfill.py`
  - `storage/sonarr_candidates.py`
  - `storage/sonarr_selection.py`
  - `storage/sonarr_dispatch.py`
  - `storage/sonarr_cached_retry.py`
  - `storage/sonarr_magnet_intake.py`
- **Genuine Sonarr integration** stays under Sonarr names: `sonarr.py` (Keep/import), `keep_*`, `post_keep_cleanup.py`, `batch_post_keep_cleanup.py` and `storage/*keep_imports.py`.
- **`sonarr_episode.py`** is shared TVDB→Sonarr episode matching, used by both Keep and discovery. RF-004 has to decide whether the discovery-side use is generic TVDB placement matching.

---

## 5. Duplication

AST-identical function bodies (excluding docstrings, bodies under about 300 AST characters ignored):

| Group | Locations | Assessment |
|---|---|---|
| Reservation loaders | `storage/batch_reservations.py:98` `load_batch_reservation` / `storage/episode_reservations.py:47` `load_reservation_input` | The wrappers are identical, but each calls its **own** `_facts()` with a different query. This is not a true duplicate. Only a shared read-only snapshot helper would remove it (see below). |
| `wait()` | `jobs/download_torrent_job.py:44`, `jobs/transfer_batch_job.py:80`, `jobs/unlock_batch_job.py:79` | 2–3 lines each. Too small to justify a shared helper. Leave. |
| `pairs()` | `acquisition/qbittorrent.py:21`, `acquisition/submission.py:105` | 6 lines. Could be consolidated if one module already imports the other (RF-003 to check). |
| `fence()` | `storage/keep_imports.py:65`, `storage/batch_keep_imports.py:135` | 2–3 lines. Leave. |
| `value()` | `configuration_schema.py:306,646` | Two snapshot classes. Leave; they are separate public types. |
| `json()` | `acquisition/metadata.py:147,187` | Two dataclasses. Leave. |
| `run()` / `route()` | `stage_batch_episode_job` / `stage_trial_media_job`, `verify_batch_episode_job` / `verify_media_job` | Parallel batch and single-episode pipelines. Leave: the responsibilities differ and the bodies are tiny. |
| `__init__` | `batch_post_keep_cleanup.py:15` / `post_keep_cleanup.py:53`; `acquisition/submission.py:93` / `storage/batch_submission_journal.py:53` | Constructor boilerplate. Leave. |

**Repeated pattern (P13).** The read-only snapshot sequence appears 16 times in 14 storage modules:

1. `_open(readonly=True)`
2. `BEGIN`
3. Check `PRAGMA user_version` against `LATEST_VERSION`
4. Map `OSError` / `sqlite3.Error` / `MigrationError` → `QueueUnavailable`
5. `close`

A shared context manager would remove about 8 lines per site, but it would be a new abstraction. The work-item rules allow one only where the surviving code needs it. **Recommendation:** list it as an optional RF-003 consolidation and don't do it by default.

Repeated helper names (`registration` ×29, `_persist` ×26, `enqueue` ×6) are a per-module convention for the handler registry, not duplication.

---

## 6. Feature flags and development-only code

A search for TODO, FIXME, HACK, deprecated, experimental, feature-flag, obsolete and superseded markers found no abandoned feature flags and no development-only branches in production code. The only "obsolete" markers are the configuration-compatibility notes listed in §3.

## 7. Superseded Sonarr acquisition functionality

Sonarr-based release search and grab was replaced by Prowlarr in `db41b589` ("search Prowlarr for backfill and continue instead of Sonarr"). The only leftover code from that era is **D1 (`add_payload`)**. Everything else under Sonarr names is either:

- live discovery code that still carries the historical prefix (rename in RF-004), or
- the genuine Keep/import integration (keep it).

---

## Summary

| Class | Count |
|---|---|
| Confirmed dead definitions | 5 (D1–D3, D5, D6). D4 was reclassified as required. |
| Confirmed unused imports | 51 names in 30 files (D7), including 4 added by #261 |
| Potentially dead (owner decision or more investigation) | 12 definitions + 1 pattern (P2–P14) |
| Required compatibility | 11 groups |
| Unreferenced modules | 0 |

**RF-003 plan:** remove D1–D3 and D5–D7 in small commits grouped by subsystem (acquisition, storage, API/worker, jobs), running focused tests after each and the full suite after each group. Leave P2–P14 for an owner decision. `prune_snapshots` (formerly P1) is kept by owner decision. See §3.
