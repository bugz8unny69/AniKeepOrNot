# Work items: the series job (branch `feat/series-job`)

Written 2026-11-23 for an AI agent starting on this branch. Read
[SERIES_JOBS_AND_RENAME.md](SERIES_JOBS_AND_RENAME.md) first: it is the plan
the owner approved. This file splits it into ordered work.

## The goal in one paragraph

Today every step of every torrent is its own job row (28 job kinds, 56 tables
that point at job numbers, 515,000 history rows for 138 series). The rework
replaces that with **one row per series** in a `series` table, keyed by AniDB
ID. The series job sleeps until something wakes it (the API with a new
release, a download finishing, the owner's action), works N torrents at a time,
keeps each torrent's step and state inside its row, and ends only when the
owner presses Keep or Reject. The database grows with the number of series,
not with the work done.

## Rules (from the owner; follow them exactly)

- **Branch:** work on `feat/series-job`. `develop` is what runs on production;
  merge `develop` into this branch often (see "Coordination" below). Never touch
  `main`, `refactor` or `NextGen`.
- **No catch-alls.** Catch only the errors a place expects and knows how to
  handle. Anything else raises and is logged with its full stack trace. No
  `except Exception: pass`, no turning an unknown error into a reason code.
  A boundary that must keep going (the worker loop) logs the full trace at
  ERROR and says what it skipped.
- **Few checks.** Too many checks create too many problems. Validate input at
  the edge (API, migration); trust the data inside.
- **Logs:** "started" then "finished" means it worked. An error logs the
  traceback. A repeated wait logs nothing.
- **Tests:** write and run the tests for each change only. Never run the full
  suite unless the owner asks. Tests live in `src/anime/tests/`; run with
  `python -m pytest -q src/anime/tests/test_<name>.py`.
- **Production tests:** real or load tests run on production over
  `ssh root@torrents.int.lhprojects.net`, in a temporary folder with a copy of
  the database (`sqlite3 … ".backup"`), never on the developer's machine. Delete
  the copy afterwards. Never call AniDB or TVDB from scripts on production.
- **Deploy only when the owner asks.** Deploy is
  `/usr/local/bin/akon-install --upgrade --revision <full sha> --yes` after pushing.
- **Migrations:** add `src/anime/storage/sql/0NN_name.sql` (guard temp table,
  `PRAGMA user_version`, see `077_identity_requests.sql`), add it to `SCRIPTS`
  and bump `LATEST_VERSION` in `storage/migrations.py`, put the same DDL in
  `SCHEMA` in `storage/sqlite.py` (the runner checks they match), and add the
  table to `tests/test_batch_recovery_migrations.py`. Migrations make no
  backup; a migration moves old data into the new shape itself.
- **Commits** end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
  and the subject ends with `[skip ci]`.
- **Writing for the owner:** short, plain sentences. Say what changed and what
  is left. Avoid long lists of internals.

## What stays and what goes

- **Stays (reuse, don't rewrite):** the libraries the steps call.
  - identity: `identity_bridge.py`, `identity_process.py`, `providers/`
  - release and season-pack checks: `batch/verification.py`, `batch/selection.py`,
    `batch/recognition.py`, `release_contracts.py`, `release_names.py`
  - torrents: `acquisition/` (metadata, submission, transfers, media, cleanup),
    `torrent_client/qbittorrent.py`, `batch/qb_client.py`
  - media and staging: `acquisition/media.py`, `staging.py`, `transcode_batch.py`
  - decisions: `keep_service.py`, `keep_selection.py`, `sonarr.py`, the reject
    logic now in `jobs/reject_series_job.py`
  - search: `acquisition/prowlarr_search.py`, `providers/backfill_catalog.py`
  - notifications: `notifications.py`, `storage/notifications.py`
- **Goes at the end (SJ-11):** `jobs/*_job.py`, `job_report.py`, `job_recovery.py`,
  `job_diagnostics.py`, the Jobs page, `storage/job_queue.py` and the per-step
  tables. Until then the old path keeps working behind a setting.

Each old `jobs/*_job.py` is the best map of how a step uses its library and
which errors it expects (see the `except` list in `jobs/verify_release_job.py`).
Read it, then call the library from the series step directly.

---

## Priority 1: the core (do in order)

### SJ-1. The `series` table and its settings

- Migration v78 creates the table from the plan (section 0): `anidb_id` primary
  key, `title`, `state` (`WORKING`, `ASLEEP`, `KEPT`, `REJECTED`), `wake_at_ms`,
  `torrents_at_once`, `torrents_json`, `episodes_json`, `recent_json`, lease
  columns, timestamps. Plus an index on `(state, wake_at_ms)`.
- Settings (`configuration_schema.py`, next to `worker.concurrency`):
  - `series.enabled` (bool, default false): new releases go to the series job.
    False keeps today's path.
  - `series.torrents_at_once` (int 1–8, default 1): the default N; a series row
    can override it.
- **Done when:** the migration and `SCHEMA` match, a migration test passes, and
  the settings show on the dashboard Settings page.

### SJ-2. The series store

New module `src/anime/series_job/store.py`. The only code that reads or writes
`series` rows.

- `claim(db, now)`: take one series that is `WORKING`, or `ASLEEP` with
  `wake_at_ms` due, and lease it (same idea as `JobQueue.claim`).
- `save(db, claim, row)`: write the row back while the lease is held.
- `wake(db, anidb_id, title)`: create the row if new; set `WORKING`.
- `add_torrent(db, anidb_id, release)`: append to `torrents_json` once per
  info hash.
- `note(row, line)`: append to `recent_json`, keeping the last 50 lines; the
  same line also goes to the log.
- Document the JSON shapes at the top of the module:
  - torrent: `infohash`, `release_name`, `raw_body`, `step`, `state`
    (`waiting`, `working`, `done`, `dropped`), `why`, `episodes`, `files`,
    `progress`, `submitted_at_ms`;
  - episode: key like `S01E03`, with `state`, `torrent`, `file`.
- **Done when:** tests cover claim, lease loss, wake, adding the same torrent
  twice (no-op), and the 50-line cap.

### SJ-3. Hand-off from the API

`identity_bridge.apply_result` today creates a `VERIFY_RELEASE` job. With
`series.enabled` on, it calls `wake` and `add_torrent` instead, in the same
transaction, and logs "gave series <aid> <title> torrent <name>". Keep the
decided-series refusal and API Reject exactly as they are.

- **Done when:** with the setting on, an identified release creates or wakes
  one series row and adds one torrent; no `jobs` row is created. With it off,
  nothing changes (the existing tests in `test_identity_bridge.py` and
  `test_api_v1.py` still pass).

### SJ-4. The worker runs series

A new loop in `src/anime/series_job/worker.py`, started by the existing worker
service next to the old loop (the old loop keeps running old jobs).

- Claim a series, pick up to N torrents that are not done or dropped, run each
  one's next step, save the row, then either loop again (more to do), sleep
  until `wake_at_ms` (waiting on a download), or sleep until woken (nothing
  left).
- Two torrents never work on the same episode at once: reserve episodes in
  `episodes_json`.
- A step is a function `async step(series, torrent, context) -> result`, where
  the result says: done (go to the next step), wait until a time, or drop the
  torrent with a reason. Steps live in `src/anime/series_job/steps/`.
- **An expected problem** (a service down, a bad torrent) is handled in the
  step: wait, or drop the torrent with its reason. **An unexpected error**
  raises: the loop logs the full trace at ERROR, marks the torrent dropped
  with "program error at file:line", sends a `JOB_FAILED` notification, and
  the series goes on with its other torrents.
- Build the steps in this order, one commit each, with a test each:
  1. **SJ-4a. Check the release:** single episode and season pack (today:
     `jobs/verify_release_job.py`, `jobs/evaluate_batch_job.py`).
  2. **SJ-4b. Reserve episodes** (today: `reserve_episode_job.py`,
     `reserve_batch_job.py`).
  3. **SJ-4c. Submit and download:** add to qBittorrent and wait for it, with
     progress in the torrent (today: `prepare_acquisition_job.py`,
     `submit_torrent_job.py`, `download_torrent_job.py`, `submit_batch_job.py`,
     `transfer_batch_job.py`, `verify_batch_probe_job.py`, `unlock_batch_job.py`).
     Waiting on a download sleeps the series (`wake_at_ms`), logs nothing
     while nothing changes, and logs progress only when it changes.
  4. **SJ-4d. Check the media** (today: `verify_media_job.py`,
     `verify_batch_episode_job.py`).
  5. **SJ-4e. Transcode** (today: `transcode_media_job.py`, `transcode_batch.py`).
  6. **SJ-4f. Stage for evaluation** (today: `stage_user_evaluation_media_job.py`,
     `stage_batch_episode_job.py`, `select_user_evaluation_job.py`).
  7. **SJ-4g. Drop a torrent:** remove it from qBittorrent and delete its files
     (today: `cleanup_batch_download_job.py`, `cleanup_canceled_operation_job.py`),
     record why, move on to the next torrent.
- **Done when:** one release goes through every step to staged in a test with
  fake services, and on production (temporary database copy, real qBittorrent
  disabled or a test category) a real series runs end to end.

## Priority 2: what the owner sees

### SJ-5. Decisions and search on the series

- **Keep:** today `keep_media_job.py`, `keep_batch_job.py`, `keep_service.py`,
  `cleanup_batch_kept_media_job.py`. Ends the series as `KEPT`; Sonarr owns it.
- **Reject:** today `reject_series_job.py`. Removes everything, ends as `REJECTED`.
- **Search:** today `discover_episode_releases_job.py`,
  `discover_batch_prowlarr_job.py`. Finds releases for missing episodes and adds
  them as torrents.
- Each is an action on the series (API endpoint by AniDB ID), done by the
  series loop, not a separate job.

### SJ-6. `adm` by AniDB ID

- `adm report intake --id <anidb_id>`: the series, its torrents with their
  step, state and why, its episodes, and its recent history.
- `adm running`: what is being worked on now (the same as the Running page).
- `adm keep|reject|search --id <anidb_id>`.
- `adm report job` is removed at SJ-11.

### SJ-7. Dashboard

- **Running page** (replaces Jobs): each series being worked on, its torrents,
  each with episode, step and download progress; releases waiting on AniDB
  (from `identity_requests`).
- **Intake page:** each series opens to its episodes and torrents, read from the
  series row.
- No job numbers anywhere. Links and buttons take the AniDB ID.
- API: `GET /api/v1/running`, `GET /api/v1/series/{anidb_id}`, and the actions
  from SJ-5.

### SJ-8. Worker log lines

One line when work starts on a series, then one per step started, finished,
or failed, naming the series, the episode and the torrent:

```
series 16999 Cyberpunk Edgerunners · S01E05 [Group] … · download started
series 16999 Cyberpunk Edgerunners · S01E05 [Group] … · downloading, 53%
series 16999 Cyberpunk Edgerunners · S01E05 [Group] … · download finished
```

## Priority 3: switch over, then clean up

### SJ-9. The one-time cutover script

`fixes/cutover_series.py`, run once by the owner. Test it first on production
in a temporary folder with a database copy.

1. Stop the services and take maintenance.
2. Turn each series with unfinished work into a series row: its torrents with
   their current step, its episodes. Production has 81 unfinished jobs, all
   on hold or in Needs review: 40 episode searches, 18 user evaluations, 15
   episode reservations, 7 batch reservations, 1 batch keep. Torrents already
   in qBittorrent keep downloading; nothing is downloaded again.
3. Series already kept or rejected get a row in that state, so a new release
   for them is refused.
4. Drop the old tables (jobs, job history, job operations, the per-step
   tables) and wipe the history. `VACUUM`.
5. Turn `series.enabled` on and start the services.

### SJ-10. Rename `anime-discovery` to `akon`

Same script or the next step of it. Table in the plan, section 5:

- services `akon-api`, `akon-worker`, `akon.target`; user and group `akon`;
- `/etc/akon`, `/var/lib/akon/akon.sqlite`, `/opt/akon`;
- qBittorrent category `akon` and its folders, moved through qBittorrent so it
  keeps seeding, with the saved paths updated;
- logger names, log lines, User-Agent;
- nginx `/akon/api/v1/…`, and the old `/anime-discovery/api/v1/webhook` and
  `/check` still passed through to AKON until Autobrr is updated;
- the installer `akon-install` learns the new names and removes the old ones.

### SJ-11. Remove the old job layer

Once production runs on series jobs: delete the code and tests listed in
"Goes at the end", the `series.enabled` setting (always on), and the old
`adm report job`.

---

## Coordination with `develop`

These are being improved on `develop` meanwhile. Merge `develop` in when they
land, and don't change the same places here before then:

- AniDB's title list moves out of the database to a file (`providers/http.py`,
  `storage/provider_cache.py`).
- qBittorrent out of disk space waits instead of canceling
  (`acquisition/transfers.py`).
- A season pack that starts after episode 1 is checked from its first episode
  (`batch/selection.py`, `batch/verification.py`).
- Download progress from qBittorrent (`torrent_client/qbittorrent.py`).
- Saved Prowlarr search results are capped (`acquisition/prowlarr_search.py`).
- Catch-alls removed from the libraries (work item 3 in
  [WORK_ITEMS_JOB_CLARITY.md](WORK_ITEMS_JOB_CLARITY.md)).
