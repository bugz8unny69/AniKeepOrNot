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

**Behavior constraint for RF-005:** `VerifiedEpisode.__post_init__` currently raises `SonarrEpisodeMismatch`. After the split it must raise a generic `EpisodeIdentityInvalid(ValueError)`. To keep every existing `except SonarrEpisodeMismatch` / `except ValueError` site behaving the same, **`SonarrEpisodeMismatch` must subclass `EpisodeIdentityInvalid`**. Today the only catch site is `keep_selection.py:48`; RF-005 re-checks this at the time of the change.

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
| **Immutable history** (`job_history.event_code`, `reason_code`, `handler_code`, `handler_label`, `jobs.last_reason_code`, old `snapshot_json`) | **Never rewrite.** Old values stay registered and readable. A read-side alias maps old to new wherever lookups key on the value. |
| **Historical migration files** (`storage/sql/042_*`–`045_*`, `migrations.py` list entries) | **Never rename or edit.** Migrations replay in order on old databases. |

### 4.2 Job kind

| Existing | New | Where persisted / used | RF-006 handling |
|---|---|---|---|
| `DISCOVER_SONARR_BACKFILL` | `DISCOVER_EPISODE_RELEASES` | `jobs.kind` (CHECK only requires a non-empty value); `job_history.handler_code` (immutable); registry key; dashboard `HandlerCatalog`; `adm recover --kind` filter; `job_recovery`; `review_retry`; `fixes/` | **Migrate `jobs.kind`** for every row, terminal ones included, so that recovery, retry and the dashboard resolve one kind. **Keep `job_history.handler_code` as written** and register a historical handler alias, so old history still displays a label. `adm recover --kind` accepts the old value as an alias of the new one. |

Why migrate every row and not just the active ones: if only active rows moved, the same capability would exist under two kinds, and every kind-keyed query would need both. One migration plus one read-side history alias is smaller and has no parallel runtime path.

### 4.3 Event source

| Existing | New | Where | RF-006 handling |
|---|---|---|---|
| `incoming_events.source = 'sonarr-discovery'` | `'release-discovery'` | Written in `storage/sonarr_dispatch.py:98`. Read by 12 queries and checks in `backfill_attempts`, `releases`, `sonarr_candidates` (×4), `sonarr_selection`, `sonarr_magnet_intake` (×2) and `sonarr_cached_retry` (×2). | **Migrate the column value.** These rows are internal deliveries created by NextGen, never external webhooks; `raw_body` is not touched. Afterwards the code uses a single shared constant instead of 13 string literals. |

### 4.4 Tables

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

These codes are stable public identifiers. `REASON_CODES.md` requires that codes are stable and never recycled, and they appear in immutable history, `jobs.last_reason_code`, logs and `review_retry`.

| Code(s) | Kind | Still emitted? | Recommendation |
|---|---|---|---|
| `SONARR_BACKFILL_OBSERVED` | event | yes (3 sites) | **Keep the code; fix the display text.** Today it reads "Read-only Sonarr backfill results recorded."; it should say something like "Episode discovery results recorded." |
| `SONARR_BACKFILL_PLAN_CONFLICT`, `SONARR_CANDIDATES_EXHAUSTED`, `SONARR_SELECTION_CONFLICT` | reason | yes | **Keep the codes; fix the display text.** |
| `SONARR_DISCOVERY_*` (8 codes), `SONARR_EPISODES_NOT_LISTED` | reason | 3 still emitted; 6 historical only | **Keep them registered.** `review_retry.py:51-52` uses them to decide whether historical jobs are eligible for retry. |

**Recommended option A:** keep all codes and correct only their display messages. **Option B:** add new generic codes for new emissions and keep the old codes registered as historical aliases. B is more consistent, but every code-keyed rule (`review_retry`, `job_recovery`, the dashboard and reports) would then have to accept both values. It adds alias surface and gains no behavior. **This is an owner decision; RF-006 assumes A unless told otherwise.**

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
2. **RF-006 next** (§4). One new migration, `059`, handles `jobs.kind`, the event source and the table renames. It also needs:
   - read-side aliases for the history handler code and the `adm recover --kind` filter;
   - display text fixes for the codes.
3. **RF-007** tests the migration from a **real pre-refactor schema fixture**, with pre-rename jobs in every state.

Risks to carry into RF-005 and RF-006:

- **SQL that mentions the old table names.** It is spread across 17 non-test modules and 21 test or `fixes/` files. The `test_sql_parameter_counts` guard does not check table names. RF-006 must grep every SQL string for the four table names and the source literal, and the grep must cover `fixes/`.
- **Upgrade safety.** A database migrated while a discovery job is `RUNNING` would end up with a lease on a renamed kind. The migration runs only under the existing maintenance gate, with the worker stopped, which is the same requirement as every migration.
- **`SonarrEpisodeMismatch` catch sites.** See §3.2. RF-005 has to keep the exception hierarchy compatible.
- **Downgrade.** Like every NextGen migration, this one is forward-only. Rolling back means restoring the pre-upgrade backup, which the existing database-upgrade flow already takes.

## 7. Open owner decisions

1. **Reason and event codes (§4.6):** option A (recommended) or option B.
2. **New names:** confirm or adjust the names in §3 and §4. They are proposals chosen from what each piece actually does. The ones worth checking most are `magnet_intake` and `magnet_intents`, because those modules serve both discovery and webhook magnets.
