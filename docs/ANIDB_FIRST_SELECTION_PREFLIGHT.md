# AniDB-first episode selection (Step 1): preflight

Status: **preflight only**. There are no schema changes and no change to the live window yet.

Spec §1 requires this preflight before any schema edits: we must know whether every existing row converts to exactly one AniDB episode.

## What is in this change

- `fixes/anidb_selection_preflight.py`: a read-only report. It opens the configured database read-only, and services can stay running:

  ```
  sudo python3 /opt/anime-discovery/current/fixes/anidb_selection_preflight.py
  ```

  Exit codes: `0` = the conversion is unambiguous, `1` = blocked (each row is listed), `2` = the file can't be read as a v54 database.

- `Window.anidb_episodes()` in `trial_policy.py`: the spec §0 window rule in AniDB numbers. Nothing calls it yet. `Window.episodes()` is unchanged, because season packs depend on it (spec §8).

## Evidence the conversion may use

An old row is keyed by (`plan_id`, `tvdb_episode`). Its placement is `(plan.anidb_id, plan.tvdb_series_id, plan.tvdb_season, tvdb_episode)`. It converts only when stored, verified rows give exactly one AniDB number:

- `recognized_episodes.anidb_episode_number` (same anime and TVDB placement), or
- `release_verifications.anidb_episode_number` (same anime and placement, `tvdb_order='official'`).

There are no network lookups, no season offsets and no release names. Plan anchors convert through `anchor_verification_id`.

**Blocking:**
- more than one plan for an `anidb_id`
- any keyed row with no evidence or more than one possible number
- two old keys that land on the same new key
- an open discovery job whose `retry_episode`, `fallback_episode` or `exhausted_episode` can't be converted

**Expected risk:** `NO_RESULTS`, `NOT_AIRED` and `MAPPING_MISSING` observation rows for episodes that were never verified have **no** stored AniDB evidence, so they will show up as blocking. Spec §1.4 and §11 say to stop there and propose a repair for approval. Nothing is dropped automatically.

## Old key → new key checklist (for migration 055)

| Table | Old key | New key | Kept unchanged |
|---|---|---|---|
| `trial_plans` | `UNIQUE(anidb_id,tvdb_series_id,tvdb_season,tvdb_order)`; `anchor_episode` = TVDB number | `UNIQUE(anidb_id)`; `anchor_episode` = AniDB number | `anchor_verification_id`, `tvdb_series_id`/`tvdb_season` as anchor metadata only, counts, revision |
| `sonarr_backfill_results` | `(plan_id, tvdb_episode ≤1000)` | `(plan_id, anidb_episode_number)`, no 1000 cap | `tvdb_episode_id`, status, counts |
| `sonarr_episode_selections` | `(plan_id, tvdb_episode)` | `(plan_id, anidb_episode_number)`, plus per-target `tvdb_season` and `tvdb_episode` | `tvdb_episode_id`, `info_hash`, `UNIQUE(plan_id,info_hash)` |
| `sonarr_episode_candidates` | `(plan_id, tvdb_episode ≤1000, rank)` | `(plan_id, anidb_episode_number, rank)` | rank, state, reason, hash |
| `sonarr_magnet_intents` | `tvdb_episode 1..1000` | `anidb_episode_number` | event/job ownership, marker, state, inventory, active-hash index |
| job payloads | `retry_episode` / `fallback_episode` / `exhausted_episode` = TVDB number | AniDB number | `plan_id`, `start` |
| `prowlarr_search_snapshots` | `season`/`episode` (no stated domain) | old rows stay as they are; new rows record the number domain in `snapshot_json` | everything |

## Code consumers of the old keys (NextGen, at 35db458)

**Window and plan:**
- `trial_policy.py` (`Window`, `TrialSource`)
- `storage/trials.py` (`_source`, `persist_trial`)
- `jobs/evaluate_trial_job.py`
- `storage/episode_reservations.py` `_facts`

**Search and catalog:**
- `providers/backfill_catalog.py` (`build_catalog`, `make_catalog_factory`)
- `acquisition/prowlarr_search.py` (`EpisodeTarget`, `EpisodeCatalog`, `backfill`, `search`, `_search`)

**Discovery storage:**
- `storage/sonarr_backfill.py`: `window`, `plan_window`, `_plan`, `schedule_first`, `persist_observations`, fallback, dub search, tried hashes
- `storage/sonarr_selection.py`: `select` has its own TVDB window test; also `require_verified_delivery` and `acquisition_identity`
- `storage/sonarr_dispatch.py`: `choice.identity.tvdb_season == plan.season`
- `storage/sonarr_candidates.py`
- `storage/sonarr_cached_retry.py`
- `storage/sonarr_magnet_intake.py`: joins on `p.tvdb_season` and `selected.tvdb_episode`
- `storage/backfill_attempts.py`: payload episode fields
- `jobs/discover_sonarr_backfill_job.py`: payload shapes

**Intake and UI:**
- `storage/series_intake.py` (`detail`, `retry_episode`)
- `api/app.py`: imports `retry_episode`
- `job_report.py`: reads candidates by episode

**Read only by `job_id` / `event_id` (keys unaffected):**
- `cancel_cleanup.py`
- `storage/cancellation.py`
- `storage/reservation_release.py`
- `storage/acquisition.py` (intent inventory)

**Out of Step 1 (must stay unchanged):**
- `batch/*`
- `storage/batch_*`
