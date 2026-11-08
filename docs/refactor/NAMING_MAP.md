# Discovery naming map (RF-004)

**Base:** `refactor` at `54889b96` (NextGen `304a76f1`). This is a design document only; no code changes.
**Consumers:** RF-005 renames the identifiers in [§3](#3-code-only-identifiers-rf-005), the ones that exist only in code. RF-006 handles the persisted identifiers in [§4](#4-persisted-identifiers-rf-006).

## 1. Principle: Sonarr vs. discovery

Since `db41b589`, episode discovery searches **Prowlarr**. Sonarr has no part in discovery, release selection, or acquisition. Sonarr is used only for the **genuine Sonarr integration**:

- **Keep / import**: `sonarr.py`, `keep_service.py`, `keep_resolution.py`, `keep_selection.py`, `jobs/keep_*`, `storage/keep_imports.py`, `storage/batch_keep_imports.py`.
- **Post-Keep cleanup**: `post_keep_cleanup.py`, `batch_post_keep_cleanup.py`, `keep_cleanup_proof.py`, `jobs/cleanup_*kept_media*`.
- **Integration configuration and credentials**: the `sonarr.*` settings, `sonarr_api_key`, `SecretStore.sonarr_api_key()` and `update_sonarr_api_key()`, the dashboard "Sonarr" integration panel, and the `'sonarr'` reveal kind.
- **Sonarr's own identifiers**: `match_sonarr_episode`, `resolve_episode`, `SonarrEpisodeMismatch` (Keep-side), and the historical `sonarr_episode_id` column.

**Everything in the list above keeps its Sonarr name.** Everything in §3 and §4 is generic discovery that carries a historical `sonarr` prefix, and gets the vocabulary below.

No project-wide search-and-replace is allowed. Every rename is listed here explicitly, together with its persistence class.

## 2. Vocabulary

| Concept | Term | Used for |
|---|---|---|
| The paged backfill and continue search for an anime's episodes | **episode discovery** | job, plan/window storage, results |
| One ranked torrent found for one episode | **release candidate** | ranked fallback list |
| The current chosen candidate for an episode | **release selection** | selection pointer |
| Handing a selected release to the acquisition pipeline as an internal delivery | **release dispatch** | internal event creation |
| Replaying a previously admitted magnet ("Search again") | **cached release retry** | saved-magnet replay |
| Hash-bound magnet admission into PREPARE | **magnet intake** | magnet intents |

These names describe what each piece does and avoid naming any provider, so they stay accurate if the search source changes again. "Backfill" and "continue" remain the names of the two discovery *modes* (`search_mode`). They are not a component name.

## 3. Code-only identifiers (RF-005)

These names are not stored anywhere. Changing them affects only imports, so they can be renamed directly, with no compatibility aliases. Every importer has to be updated, **including `fixes/` and `src/nextgen/tests/`**.

### 3.1 Modules

**Done in RF-005.** Every module below is renamed, with its importers, `fixes/` and `mock.patch` targets, and recorded in `removed.json`.

| Existing | New | Importers* | Notes |
|---|---|---|---|
| `jobs/discover_sonarr_backfill_job.py` | `jobs/discover_episode_releases_job.py` | 8 | Holds `KIND`. The job kind value itself is covered in §4. |
| `storage/sonarr_backfill.py` | `storage/episode_discovery.py` | 13 | The internal names (`BackfillBatch`, `FallbackTarget`, `DubSearch`, …) are already generic and stay. |
| `storage/sonarr_candidates.py` | `storage/release_candidates.py` | 7 | |
| `storage/sonarr_selection.py` | `storage/release_selection.py` | 7 | |
| `storage/sonarr_dispatch.py` | `storage/release_dispatch.py` | 3 | |
| `storage/sonarr_cached_retry.py` | `storage/cached_release_retry.py` | 6 | |
| `storage/sonarr_magnet_intake.py` | `storage/magnet_intake.py` | 7 | Admits both backfill-selected and webhook magnets, so the module is not discovery-only. |
| `sonarr_episode.py` | **split** (see §3.2) | 14 | |

\* Importing files at the time of writing, counted across `src/nextgen` and `fixes/`.

`acquisition/legacy_magnet.py` keeps its name. "Legacy" there means the magnet format (no BEP3 manifest), not a Sonarr dependency.

### 3.2 Splitting `sonarr_episode.py`

Its docstring says "shared by discovery and Keep", but the two halves do different jobs:

| Symbol | Used by | Destination |
|---|---|---|
| `VerifiedEpisode` | discovery: `prowlarr_search`, `storage/sonarr_selection`, `storage/sonarr_cached_retry`, and `storage/sonarr_backfill` (×3); also `keep_selection` | **New module `episode_identity.py`.** It is a generic verified TVDB episode identity. |
| `resolve_episode`, `SonarrEpisodeMismatch` | Keep only (`keep_selection.match_sonarr_episode`) | **Stay in `sonarr_episode.py`.** This is genuine Sonarr matching. |

**Done in RF-005.** `VerifiedEpisode` and the new `EpisodeIdentityInvalid(ValueError)` are in `episode_identity.py`; `sonarr_episode.py` keeps `resolve_episode` and `SonarrEpisodeMismatch`, which now subclasses `EpisodeIdentityInvalid`.

Correction to the earlier plan: subclassing alone does **not** keep `except SonarrEpisodeMismatch` working, because that clause does not catch the parent class that `VerifiedEpisode` now raises. The only such catch site, `keep_selection.match_sonarr_episode`, therefore catches `EpisodeIdentityInvalid`, which covers both errors, so Keep behaves as before (tested in `test_keep_selection`). `except ValueError` sites are unaffected.

### 3.3 Classes, functions and constants

None of the classes, functions or constants inside the discovery modules contain "Sonarr". They are already generic and are **not renamed**.

| Existing | New | Notes |
|---|---|---|
| `providers/configuration.py` aliases `BATCH_SUBMIT_KIND` etc. | unchanged | These are not Sonarr-related. |
| Module docstrings that say "Table and job names keep their historical 'sonarr' prefix" | Update in RF-005/RF-008 | They must say whatever RF-006 decides. |

### 3.4 Tests and fixtures

| Existing | New |
|---|---|
| `tests/test_sonarr_magnet_intake.py` | `tests/test_magnet_intake.py` |
| `tests/test_sonarr_magnet_cancellation.py` | `tests/test_magnet_intake_cancellation.py` |
| `tests/test_keep_sonarr.py` | **unchanged** (genuine Sonarr Keep) |
| `tests/test_backfill_*.py` | unchanged ("backfill" is a discovery mode) |

Test assertions on persisted values follow §4. A test may only change an expected literal when RF-006 deliberately changes that value. No assertion may be loosened.

## 4. Persisted identifiers (RF-006)

These names are stored in user databases. A rename must be **versioned, repeatable and safe for historical rows**.

### 4.1 Strategy rule

| Class | Rule |
|---|---|
| **Mutable operational state** (job rows, schema object names, internal event source) | Migrate in one versioned migration. Afterwards a single runtime path uses only the new value. There are no parallel implementations. |
| **History and stored codes** (`job_history`/`event_history` event, reason and handler codes; `jobs.last_reason_code`; codes stored inside JSON) | **Rewrite in the same migration** (owner decision, 2026-11-04). After the migration, the code recognizes only the new values. Old names are removed, with **no read-side aliases**. See [REASON_CODE_AUDIT.md](REASON_CODE_AUDIT.md) §3 for every column involved. |
| **Historical migration files** (`storage/sql/042_*`–`045_*`, `migrations.py` list entries) | **Never rename or edit.** Migrations replay in order on old databases. |

### 4.2 Job kind

**Done in RF-006a** (migration `063`), for all three renamed kinds (also `EVALUATE_TRIAL` and `STAGE_TRIAL_MEDIA`, §8.2).

| Existing | New | Where persisted / used | RF-006 handling |
|---|---|---|---|
| `DISCOVER_SONARR_BACKFILL` | `DISCOVER_EPISODE_RELEASES` | `jobs.kind` (CHECK only requires a non-empty value); `job_history.handler_code` (immutable); registry key; dashboard `HandlerCatalog`; `adm recover --kind` filter; `job_recovery`; `review_retry`; `fixes/` | **Migrate `jobs.kind` and `job_history.handler_code`** for every row, terminal ones included. Recovery, retry, the dashboard and History then see only the new kind. `adm recover --kind` accepts only the new value; the old one is removed. |

Why migrate every row and not just the active ones: if only active rows moved, the same capability would exist under two kinds, and every kind-keyed query would need both. One migration over all rows leaves a single name everywhere.

### 4.3 Event source

**Done in RF-006a** (migration `063`). One deviation from the plan below: the value stays a literal in the SQL rather than a shared constant. Job kinds and the other source, `'manual-release'`, are also written as literals in the same statements, and the literal SQL is what `test_sql_parameter_counts` checks. The ledger rule `RF-006a` stops the old value from coming back.

| Existing | New | Where | RF-006 handling |
|---|---|---|---|
| `incoming_events.source = 'sonarr-discovery'` | `'release-discovery'` | Written in `storage/sonarr_dispatch.py:98`. Read by 12 queries and checks in `backfill_attempts`, `releases`, `sonarr_candidates` (×4), `sonarr_selection`, `sonarr_magnet_intake` (×2) and `sonarr_cached_retry` (×2). | **Migrate the column value.** These rows are internal deliveries created by NextGen, never external webhooks; `raw_body` is not touched. Afterwards the code uses a single shared constant instead of 13 string literals. |

### 4.4 Tables

**Done in RF-006a** (migration `063`), together with the four "trial" tables, the column and the two indexes from §8.2.

| Existing | New | Notes |
|---|---|---|
| `sonarr_backfill_results` | `episode_discovery_results` | `ALTER TABLE … RENAME TO` (SQLite updates foreign-key references). |
| `sonarr_episode_candidates` | `episode_release_candidates` | |
| `sonarr_episode_selections` | `episode_release_selections` | |
| `sonarr_magnet_intents` | `magnet_intents` | `idx_magnet_active_hash` is already generic and stays. |

Historical intermediate names (`*_v43`, `*_v48`, `*_v53`, `*_v56`, `*_v57`, `sonarr_magnet_intents_next`) appear only inside historical migration files and stay there unchanged.

### 4.5 Columns and status values

| Existing | Decision | Reason |
|---|---|---|
| `episode_discovery_results.sonarr_episode_id` | **Keep the name; document it as historical.** | All three current INSERTs write `NULL`. Historical rows may hold **actual Sonarr episode IDs**, so the Sonarr name is accurate. Dropping it would need a table rebuild for no gain. |
| Status values `NOT_LISTED`, `HAS_FILE`, `IDENTITY_UNKNOWN` | **Keep in the CHECK constraint.** | Nothing produces them any more, but historical rows can hold them. |

### 4.6 Reason and event codes

**Done in RF-006b** (migration `064`): renames, removed codes and the new wording. The retired per-service codes were retired in RF-006c (migration `065`).

**Decided (2026-11-04): rename and migrate; old names removed.** The full audit of every reason and event code is in [REASON_CODE_AUDIT.md](REASON_CODE_AUDIT.md), with the data in [reason_code_proposals.py](reason_code_proposals.py). It covers the renamed codes, the removed codes and the code each one migrates to, and new human-readable wording for every code.

- Renamed: `SONARR_BACKFILL_PLAN_CONFLICT` → `EPISODE_DISCOVERY_PLAN_CONFLICT`, `SONARR_CANDIDATES_EXHAUSTED` → `RELEASE_CANDIDATES_EXHAUSTED`, and event `SONARR_BACKFILL_OBSERVED` → `EPISODE_DISCOVERY_RECORDED`. `SONARR_SELECTION_CONFLICT` becomes `RELEASE_SELECTION_CONFLICT` (NextGen #283 has since registered it) and its season counterpart `BATCH_SELECTION_CONFLICT` becomes `SEASON_PACK_SELECTION_CONFLICT`.
- Removed (historical Sonarr-based discovery): every `SONARR_DISCOVERY_*` code and `SONARR_EPISODES_NOT_LISTED`. Their stored rows migrate to one historical code, `EPISODE_DISCOVERY_OUTDATED`, which `review_retry.py` keeps retry-eligible (owner decision, 2026-11-06).
- Retired (no product names in codes, owner decision 2026-11-06): the per-service codes (`PROWLARR_*`, `SHOKO_*`, `JELLYFIN_*`, `DOWNLOAD_CLIENT_*`, `PROVIDER_*` and others) become the generic `SERVICE_SETUP_INVALID`, `SERVICE_UNAVAILABLE` and `SERVICE_RESPONSE_INVALID`, with the service in the details. Domain outcomes get role names, for example `RELEASE_WITHDRAWN`. This is a behavior change made on `refactor` only; see REASON_CODE_AUDIT §1a and §6.

### 4.7 Configuration and secrets

There are no changes. The v10 configuration upgrade already moved `sonarr.discovery_*` to `prowlarr.*` (`configuration_schema.py:493-558`). That upgrade code is historical compatibility and stays. The remaining `sonarr.*` keys and `sonarr_api_key` are genuine Sonarr integration.

## 5. Documentation

| File | Action (RF-008) |
|---|---|
| `docs/BACKFILL_BEHAVIOR.md`, `src/nextgen/architecture/*`, `src/nextgen/schema/README.md`, `src/nextgen/storage/MIGRATIONS.md` | Update to the new names. Mention the old names once, as the historical identifiers in the migration. |
| `docs/nextgen-sonarr-acquisition-status.md`, `docs/sonarr-qbittorrent-handoff-completion.md`, `docs/INTEGRATION_ROLLOUT.md` | **Historical.** Preserve them as they are and add a one-line banner saying discovery now uses Prowlarr. |
| `src/nextgen/KEEP_SONARR.md`, `POST_KEEP_CLEANUP.md`, `SECRETS.md`, `CONFIGURATION.md`, `SERVICE_SETUP.md` | Unchanged; these cover the genuine Sonarr integration. |
| `docs/refactor/*` | Keep the old names; these are records. |

## 6. Ordering and risks

1. **RF-005 first** (§3, code-only). A pure rename and module split, with no data changes. It runs with the full suite, `fixes/` tests and entrypoint imports.
2. **RF-006 next** (§4). One new migration, `063` (`060`–`062` are now the batch candidate tables and `service_status`), handles `jobs.kind`, the event source and the table renames. It also needs:
   - rewriting the stored codes and handler codes in history (REASON_CODE_AUDIT §3), and removing the old names from the code (no aliases);
   - display text fixes for the codes.
3. **RF-007** tests the migration from a **real pre-refactor schema fixture**, with pre-rename jobs in every state.

Risks to carry into RF-005 and RF-006:

- **SQL that mentions the old table names.** It is spread across 17 non-test modules and 21 test or `fixes/` files. The `test_sql_parameter_counts` guard does not check table names. RF-006 must grep every SQL string for the four table names and the source literal, and the grep must cover `fixes/`.
- **Upgrade safety.** A database migrated while a discovery job is `RUNNING` would end up with a lease on a renamed kind. The migration runs only under the existing maintenance gate, with the worker stopped, which is the same requirement as every migration.
- **`SonarrEpisodeMismatch` catch sites.** See §3.2. RF-005 has to keep the exception hierarchy compatible.
- **Downgrade.** Like every NextGen migration, this one is forward-only. Rolling back means restoring the pre-upgrade backup, which the existing database-upgrade flow already takes.

## 7. Open owner decisions

1. ~~Reason and event codes (§4.6)~~: **decided** (rename, migrate history, remove old names). The remaining decisions are in REASON_CODE_AUDIT §5.
1. ~~"Trial" vocabulary~~: **decided**. It becomes "user evaluation" (`USER_EVALUATION`) everywhere it can; see §8.
2. **New names:** confirm or adjust the names in §3 and §4. They are proposals chosen from what each piece actually does. The ones worth checking most are `magnet_intake` and `magnet_intents`, because those modules serve both discovery and webhook magnets.

## 8. "Trial" → "user evaluation"

**Decided (owner, 2026-11-04):** "trial" was a bad term. It is replaced by **user evaluation** (`USER_EVALUATION` / `user_evaluation`) wherever it can be. The concept is the first episodes of a new anime that are downloaded so the user can evaluate it before choosing Keep or Reject. The same rules as §4 apply: rename, migrate stored values, remove the old names, no aliases.

### 8.1 Code-only identifiers (RF-005)

**Done in RF-005**: modules, classes, functions, local names, tests and doc file names. Only Python names and module paths changed; every string value (job kinds, tables, columns, codes, settings keys, the API field) is unchanged until RF-006. Comments and docstrings that mention "trial" are updated with RF-006, so they match the renamed tables and codes. `trial_evaluation` became `user_evaluation_check`, matching the planned table `user_evaluation_checks`.

| Existing | New |
|---|---|
| `trial_policy.py` | `user_evaluation_policy.py` |
| `storage/trials.py` | `storage/user_evaluations.py` |
| `storage/batch_trials.py` | `storage/batch_user_evaluations.py` |
| `batch/durable_trial.py` | `batch/user_evaluation.py` |
| `jobs/evaluate_trial_job.py` | `jobs/select_user_evaluation_job.py` |
| `jobs/stage_trial_media_job.py` | `jobs/stage_user_evaluation_media_job.py` |
| `TrialConflict`, `TrialProposal`, `TrialSource` | `UserEvaluationConflict`, `UserEvaluationProposal`, `UserEvaluationSource` |
| `BatchTrialSource`, `BatchTrialProposal`, `propose_batch_trial` | `BatchUserEvaluationSource`, `BatchUserEvaluationProposal`, `propose_batch_user_evaluation` |
| `persist_trial`, `load_trial_source`, `persist_batch_trial`, `load_batch_trial_source` | `persist_user_evaluation`, `load_user_evaluation_source`, `persist_batch_user_evaluation`, `load_batch_user_evaluation_source` |
| `plan_batch_trial_window` (potentially dead, P2) | `plan_batch_user_evaluation_window`, unless P2 removes it first |
| Local names and parameters: `trial`, `trial_kind`, `trial_source`, `trial_registration`, `TRIAL_KIND` | The same with `user_evaluation` |
| Tests: `test_trial_policy.py`, `test_trial_migration.py`, `test_batch_trial_job.py` | `test_user_evaluation_policy.py`, `test_user_evaluation_migration.py`, `test_batch_user_evaluation_job.py` |
| Docs: `TRIAL_POLICY.md`, `jobs/STAGE_TRIAL_MEDIA.md`, `architecture/INTAKE_TO_TRIAL.md` | `USER_EVALUATION_POLICY.md`, `jobs/STAGE_USER_EVALUATION_MEDIA.md`, `architecture/INTAKE_TO_USER_EVALUATION.md` |

### 8.2 Persisted identifiers (RF-006)

**Done in RF-006a** (migration `063`): the job kinds, tables, column, indexes and the API field. The `TRIAL_*` codes follow in RF-006b.

| Existing | New | Where | Handling |
|---|---|---|---|
| Job kind `EVALUATE_TRIAL` | `SELECT_USER_EVALUATION` | `jobs.kind`, `job_history.handler_code`, registry, dashboard, `adm` filters | Migrate every row (as in §4.2). |
| Job kind `STAGE_TRIAL_MEDIA` | `STAGE_USER_EVALUATION_MEDIA` | same | same |
| Table `trial_plans` | `user_evaluation_plans` | about 40 references; foreign keys from several tables | `ALTER TABLE … RENAME TO` (SQLite updates foreign-key references). |
| Table `trial_evaluations` | `user_evaluation_checks` | holds the per-episode result (`USER_EVALUATION_SELECTED` and so on) | rename |
| Table `batch_trial_plans` | `batch_user_evaluation_plans` | | rename |
| Table `batch_trial_files` | `batch_user_evaluation_files` | | rename |
| Column `acquisition_budgets.trial_plan_id` | `user_evaluation_plan_id` | | `ALTER TABLE … RENAME COLUMN` |
| Indexes `idx_trial_evaluations_plan`, `idx_batch_trial_by_episode` | `idx_user_evaluation_checks_plan`, `idx_batch_user_evaluation_by_episode` | | drop and recreate under the new name |
| Codes `TRIAL_*` | `USER_EVALUATION_*` | | See [REASON_CODE_AUDIT.md](REASON_CODE_AUDIT.md). |
| API field `trial_plan_ready` (`api/app.py:442`, `dashboard.js`) | `user_evaluation_plan_ready` | API response to the dashboard | Rename the server and dashboard together. |

### 8.3 Settings (configuration upgrade, not just a DB migration)

**Done in RF-006d**: settings version 15 (`_v14_to_v15`), and the dashboard section is "Evaluation episodes".

| Existing key | New key |
|---|---|
| `trial.backfill_count` | `user_evaluation.backfill_count` |
| `trial.following_count` | `user_evaluation.following_count` |
| `storage.trial_root` | `storage.user_evaluation_root` |

These keys live in the user's settings file, not in the database, so they need a **new configuration schema version**. Its upgrade step moves the values to the new keys, the same way the v10 upgrade moved `sonarr.discovery_*` to `prowlarr.*`. The dashboard settings section `'trial'` (shown as "Intake selection") is renamed with it. The upgrade code that validates **older** published settings versions keeps the old key names, because it has to read old files.

### 8.4 Must keep the old name

- The historical migration files `storage/sql/011_trial_windows.sql` and `028_batch_trial_plans.sql`, and their entries in `migrations.py`. They replay on old databases.
- The validation code for older settings versions in `configuration_schema.py` that reads `trial.*` and `storage.trial_root` from old files.
- Historical documents in `docs/` that describe archived behavior.

### 8.5 Dashboard wording

**Done in RF-006a**; the settings section label changed with the settings upgrade in RF-006d.

| Existing | New |
|---|---|
| "N episode(s) planned for the trial." | "N episode(s) picked for your evaluation." |
| "No trial plan has been committed for this pack." | "No episodes have been picked for evaluation from this pack yet." |
| "Trial planned" / "Outside trial" | "Picked for evaluation" / "Not picked" |
| Settings section "Intake selection" | "Evaluation episodes" |

