# Work items: the one-time cutover to the series job (item 2)

Status: open. Written 2026-11-26. Branch: `feat/series-job` (PR 349; do not merge it).

## What this is

Production runs the old per-step jobs (database at v78). `feat/series-job` replaces them with one
row per series (`series` table, migration 079) and drops every old job and step table (migration 080).
Migration 080 **refuses to run while any old job is unfinished** (state not DONE, CANCELED or FAILED).

The cutover script (`fixes/cutover_series_job.py`) is the bridge. It runs once, with the services
stopped, on the production database. It turns everything still in flight into series rows **that the
series job can continue from**, then closes the old jobs so 080 can drop their tables.

The current script is a first draft and has to be rebuilt (CO-1 explains why). Read these first:

- `docs/SERIES_JOBS_AND_RENAME.md`: the plan and the owner's decisions.
- `src/anime/series_job/store.py`: the row and its JSON shapes.
- `src/anime/series_job/steps/*.py`, `keep.py`, `reject.py`, `search.py`: what each step reads from a
  torrent entry. **A converted row is only correct if these steps can run on it unchanged.**
- `src/anime/storage/sql/001…078`: the old tables (on `feat/series-job` the old Python modules are
  deleted; `git show develop:src/anime/storage/<name>.py` shows how the old code read them).

## Rules (from the owner)

- Work on `feat/series-job`. Never touch `develop`, `main`, `refactor` or `NextGen`. Don't merge PR 349.
- Run the tests for what you change (`python -m pytest -q <files>`), never the whole suite.
- No catch-alls. Catch only the errors you expect and can handle; anything else must raise with
  its full trace. No `except ...: pass`.
- Production tests run **on a copy of the database in a temporary folder on the server**, never on
  the live database, and never call AniDB or TVDB from a script (use cached data only).
  Host: `ssh root@torrents.int.lhprojects.net`. Live database (read-only for you):
  `/var/lib/anime-discovery/anime-discovery-develop.sqlite`.
- Don't deploy, and don't run the cutover on the live database. The owner does that.
- Commits: the subject ends with `[skip ci]`; the message ends with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Push `feat/series-job` when tests pass.
- Out of scope: the rename to `akon` (services, user, folders, qBittorrent category). That is item 3.
  Remove `--migrate-qb` and `update_saved_paths` from this script; item 3 will own them.

## The target: what a converted row must contain

A series row (see `store.py` and the steps) is:

- `anidb_id`, `title`, `state` (WORKING / ASLEEP / KEPT / REJECTED), `wake_at_ms`, `torrents_at_once`.
- `torrents_json`: a list. Each torrent:
  - `infohash` (40 lowercase hex, required), `release_name`, `raw_body` (the normalized webhook JSON:
    `release_name`, `torrent_infohash`, optional `magnet_uri`).
  - `identity`: `{"anime": {"anidb_id", "tvdb_series_id", "title"}}` (the release check needs the
    TVDB series).
  - `step` (`get_metadata`, `check_release`, `reserve_episodes`, `download`, `check_media`,
    `transcode`, `stage`, `done`) and `state` (`waiting`, `working`, `done`, `dropped`, `stopped`).
  - `qb`: `{"marker": <32 hex>, "added": "sent", "selected": true, "started": true}` for a torrent
    AKON already put in qBittorrent. The folder is `<download root>/ad-<marker>` and the tag is
    `ad-<marker>`; **reuse the old marker** so the series job still owns the torrent.
  - `files`: one entry per chosen video: `index`, `path` (inside the torrent), `size`, `episode`
    (`S01E05`), `anidb_episode_number`, `anidb_episode_id`, `tvdb_episode_id`, and when known
    `local_path`, `media` (`sha256`, `duration_ms`, `languages`, `root`, `file`), `staged_file`.
  - `staging`: `{"plans": {code: plan}, "artifacts": [...]}` for a staged torrent.
- `episodes_json`: `{code: {"state": reserved|downloading|verified|staged|kept|dropped,
  "torrent": <infohash>, "file": <path inside the staging folder>}}`.
- `work_json`: `{}`; or `search` (trial episodes still wanted), `keep` (a Keep under way).

## Work items, in order

### CO-1. Rebuild the inspection from the real old schema (P1)

The draft guesses JSON keys (`manifest.get("anidb_id")`, `staged_path`…) that the old manifests may
not have. It swallows errors (`except (sqlite3.Error, json.JSONDecodeError): pass`), and it gives
staged episodes an empty torrent hash. Replace it:

- Read each old table by its real columns (migrations 001–078; the old storage modules on `develop`
  show the joins: staging_publications → acquisition_plans → episode_reservations, and the batch_*
  equivalents through batch_episode_reservations).
- Build the per-series view with the infohash, marker, identity (from identity_results, or
  identified_anime and recognized_series for the TVDB series), files and episodes.
- A row that can't be converted is **reported with its job ID and why**, never silently skipped.
- Output a preview (no `--apply`): per series, what will be created; per job, what happens to it.

Done when the preview on a production copy lists every unfinished job with an outcome or a reason.

### CO-2. Staged episodes waiting for your decision become series that can be kept (P1)

The most important case: episodes staged for you to watch, with no Keep or Reject yet. After the
cutover, pressing Keep must import them through Sonarr (`series_job/keep.py`), and Reject must
remove them (`reject.py`).

- Each staged episode becomes `episodes[code] = {"state": "staged", "torrent": <hash>, "file": <path
  inside the staging folder>}`.
- Its torrent entry has `step: "done"`, `state: "done"`, the `files` entry with
  `anidb_episode_id`, `tvdb_episode_id` and `staged_file` (the media receipt's file signature from
  the old staging publication, which Keep compares).
- Its `staging.plans[code]` is the old staging plan (its entries list the media and NFO paths;
  Reject deletes them).
- Its `qb` marker is the old submission's marker, so Reject and Keep's cleanup can remove the torrent.

Test: a fixture database at v78 with one staged single episode and one staged season-pack episode.
After the cutover, run the real `run_keep` with a fake Sonarr (see `tests/test_series_keep.py`) and
the real `run_reject` with a fake qBittorrent (`tests/test_series_steps.py`'s `FakeQb`).

### CO-3. Work still in progress resumes at the right step (P1)

For each unfinished old job, choose the series torrent's step:

| Old work | Series torrent |
|---|---|
| Release not verified yet (VERIFY_RELEASE, EVALUATE_BATCH) | `step: get_metadata`, `state: waiting` |
| Reserved, not submitted (RESERVE_*, PREPARE_ACQUISITION, SELECT_USER_EVALUATION) | `step: get_metadata` (metadata is fetched again; nothing was sent) |
| Submitted or downloading (SUBMIT_*, DOWNLOAD_TORRENT, TRANSFER_BATCH, UNLOCK_BATCH) | `step: download`, `qb` from the old marker, `added/selected/started` from the old journal phases |
| Downloaded, not checked or staged (VERIFY_MEDIA, VERIFY_BATCH_*, TRANSCODE_MEDIA, STAGE_*) | `step: check_media` (checks are cheap and safe to repeat) |
| Episode search (DISCOVER_EPISODE_RELEASES, DISCOVER_BATCH_PROWLARR, RESET_EPISODE_SEARCH) | no torrent; add the AniDB episode numbers to `work.search.wanted`, `due_ms` now |
| Keep under way (KEEP_MEDIA, KEEP_BATCH) | `work.keep` at the matching step (see CO-4) |
| Cleanups (CLEANUP_*) and housekeeping (REFRESH_JELLYFIN, LINK_SHOKO, FIND_DUBS_FOR_SONARR) | closed, with a log line; the next Keep or Reject redoes cleanup |

A job in NEEDS_REVIEW (or BLOCKED) gives a torrent with `state: "stopped"` and `why` set to its last
reason code, so Retry resumes it and you can see why it stopped.

A job ON_HOLD is not stopped: the series job has no hold (owner, 2026-11-27). One job per series
knows what it is working on, so held work converts as waiting work; a held job of a kept or
rejected series closes with no torrent. A job FAILED by a program error (JOB_HANDLER_FAILED)
converts stopped, so Retry recovers it.

For downloads, read qBittorrent's view **only if the owner allows it during the run**. Otherwise
trust the journal phases, and let the download step's own checks sort it out (it reads the torrent
back before acting, and never adds a torrent twice: `qb.added` is set).

### CO-4. Decisions become KEPT or REJECTED rows (P1)

- `anime_review_decisions` ACCEPT → series `state: KEPT`; REJECT → `state: REJECTED`; staged
  episodes of a kept series → `kept`.
- A Keep that was under way (a KEEP_* job not DONE) → `state: WORKING` with
  `work.keep = {"requested_at_ms", "step", "imports": {...}}`, the step and the per-episode import
  phases taken from `keep_imports` / `batch_keep_imports`. A phase of IMPORT_UNCERTAIN keeps its
  `command_id`, so nothing is imported twice.
- Converted decisions must make `storage/review_decisions.decision()` give the same answer as before
  for every AniDB ID that had a decision (test this on the production copy).

### CO-5. Close the old work so migration 080 can run (P1)

- In the same transaction as the conversion, set every converted unfinished job to CANCELED
  (`last_reason_code` 'CUTOVER_CONVERTED'; register the reason code in `reasons.py` and in
  `docs/refactor/reason_code_proposals.py`, or reuse an existing one).
- Don't drop tables in the script. Migration 080 drops them, and checks the result.
- Remove the script's own `SERIES_TABLE_SQL`. The database must reach v79 through the real
  `079_series.sql` (the migration runner), never through a copy that can drift.
- Order of the run, which `--apply` enforces:
  1. Refuse unless the API and worker are stopped.
  2. Back up the database next to it (0600).
  3. Apply migration 079 if the database is at v78.
  4. Convert and close the old work.
  5. Print the report.
  6. The owner then runs the installer, which applies 080.

Test: a v78 fixture with unfinished jobs → `--apply` → migration 080 runs cleanly →
`PRAGMA foreign_key_check` is empty → the structure equals a new database (`migrations._structure`).
The same fixture without `--apply` changes nothing.

### CO-6. Rehearse on a production copy (P1)

On the server, in a temporary folder: copy the live database (`sqlite3 … ".backup /tmp/x/akon.sqlite"`),
run the preview, then `--apply` on the copy, then migration 080 on the copy (the migration runner, or
`executescript` of `080_series_only.sql`). Then check:

- Every unfinished job is either converted or reported with a reason.
- Every staged episode is in a series row with its file, torrent, marker and staging plan.
- `decision()` gives the same answer as before for every decided AniDB ID.
- For a sample of staged episodes, the `file` exists under the staging folder and its size matches
  `staged_file`.
- For a sample of converted downloads, the `ad-<marker>` folder exists under the download root
  (read-only check; don't call qBittorrent's API from the rehearsal).

Write the rehearsal's numbers (series created, torrents per step, jobs closed, rows refused and why)
into the PR description, then delete the temporary folder.

### CO-7. Fix and extend the script's tests (P2)

`fixes/test_cutover_series_job.py` doesn't collect now: it builds old-table data on a fresh database,
which no longer has those tables. Build its fixtures at v78 from the migration scripts instead
(`for p in migrations.SCRIPTS[:78]: db.executescript(...)`, as `tests/test_series_only_migration.py`
does), and cover CO-2 to CO-5.

## Done when

- The preview and `--apply` work on a production copy, with the CO-6 checks written up in PR 349.
- Migration 080 runs on the converted copy.
- A converted staged series can be kept and rejected by the real series code (CO-2 tests).
- The script's tests pass, and no catch-all remains in the script.
