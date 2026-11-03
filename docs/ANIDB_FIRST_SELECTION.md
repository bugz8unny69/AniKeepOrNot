# AniDB-first episode selection (Step 1)

Status: **implemented** by migration 056 (`storage/sql/056_anidb_first_selection.sql`).
Behavior is described in `BACKFILL_BEHAVIOR.md`.

## What changed

- One trial plan per AniDB anime. `trial_plans.anchor_episode` is the verified
  AniDB episode number. TVDB series/season stay as anchor metadata only.
- The single-episode window is in AniDB numbers (`Window.anidb_episodes`).
  Season packs keep `Window.episodes` and are unchanged.
- Plan-scoped keys are AniDB episode numbers (`anidb_episode_number`) in
  `sonarr_backfill_results`, `sonarr_episode_selections`,
  `sonarr_episode_candidates`, `sonarr_magnet_intents`, and in discovery job
  payloads (`retry_episode`, `fallback_episode`, `exhausted_episode`).
- Each selection stores its own verified TVDB placement (`tvdb_season`,
  `tvdb_episode`, `tvdb_episode_id`).
- Intake lists missing episodes by AniDB number. The verified TVDB `SxxEyy` is a
  label when known. Search again is `POST .../backfill/<anidb_episode>/retry`.

## Migration 056

It converts keys using only stored, verified evidence (`recognized_episodes`,
`release_verifications`). There are no network lookups and no season offsets.

**Drops:** search-result rows (`sonarr_backfill_results`) that can't be converted.
They are derived observations, and Search again recomputes them.

**Aborts and leaves v55 unchanged when:**
- an anime has more than one plan
- an anchor doesn't match its verification
- evidence is ambiguous
- two old keys would land on one new key
- any selection, candidate, magnet intent or open discovery job payload can't
  be converted

Finished jobs without evidence keep their historical payload.

## Still Step 2

Reservations, staging, Intake grouping, `recognized_episodes` and manual
submissions are still TVDB-keyed. An episode without a TVDB placement can't be
acquired yet.
