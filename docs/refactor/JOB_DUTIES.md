# One job, one duty, many activities

Status: **design, awaiting owner approval**. No code changes yet.
Branch: `refactor`. The owner's rule is that NextGen keeps only fixes, and behaviour
changes that improve things land on `refactor` and are listed in the PR.

## The owner's goals

1. **The worker directs.** What arrives through the API becomes one job with
   one duty. The job carries it through its lifecycle, and its steps are
   activities that say what they are doing.
2. **Accept everything.** autobrr's request is always accepted, with no error
   and no rejection reply. What happens next is the job's lifecycle.
3. **Nothing needs review.** NextGen's biggest problem is that work ends up in
   Needs review, so "it's working" while no new episodes appear. On `refactor`, no
   job ever stops to wait for a person.
4. **No loops, no stale jobs.** A job never repeats itself. The worker
   supervises every job; a job that has done nothing since the last check is
   fired.
5. **One interface for files; adapters for torrent clients.** Every job
   creates, moves and deletes files through the same interface, which records
   everything it did, shows the work done on each file and cleans up. Torrents
   go through the qBittorrent **adapters**. They only talk to the client, never touch
   files, and all report the files each torrent uses in the same format. No extra
   "prove we own it" check, no leftovers.
6. **Waits are defined and gentle.** A download is followed in qBittorrent
   while it downloads, without stressing qBittorrent's API.
7. **Logging is cheap and respects the configured level.** Writing a log or
   a History row has a cost, so only what changed is written.

## Terms

| Term | Meaning |
|---|---|
| **Worker** | The director. It takes what the API accepted, assigns the duty, gives it to a job and runs that job. |
| **Job** | One job per piece of work accepted from the API, created at acceptance. It owns that work from start to finish and never launches other jobs. |
| **Duty** | What the job must get done: `EPISODE`, `SEASON_PACK`, `BATCH`, `RESET_EPISODE`, … |
| **Activity** | One step of the lifecycle: *Identifying*, *Verifying release*, *Downloading 42%*, … The job always shows its current activity and what it is doing. |

## 1. Accept, and the job exists

* The API accepts autobrr's request (`202`) and **creates the job in the same
  transaction**. Nothing is ever accepted without an owner.
* The first activity is **Identifying**: the full identity check (the release
  name parsing and AniDB/TVDB). Its result:
  * identified: the worker assigns the duty (episode, season pack or batch)
    and the job continues;
  * a series you rejected (autobrr re-sends), unidentifiable, conflicting or
    unsupported type: the job **ends `REJECTED`**. This is a normal ending, like
    Done, with the reason. It is not an error, a hold or a review. The Intake page
    shows re-sends of a rejected series as a count, not one entry each;
  * an identity service is down: the job **waits** for it (below).
* Release-name parsing, now spread over `acquisition/release_titles.py`,
  `providers/scoring.py` and `providers/parsing.py`, moves into one pure
  module, `release_names.py`. It has no database and no network access, and
  everything that reads a release name uses it.

## 2. The lifecycle: every state has a deadline

```
ACCEPTED ─► RUNNING (activity) ◄──► WAITING (why, until when)
                │
                ├─► DONE
                ├─► REJECTED      the release or series is not wanted (normal ending)
                ├─► CANCELED      by you, or by a reset
                ├─► FAILED        gave up after its deadline; cleanup has run
                └─► FIRED         did nothing while supervised; cleanup has run
```

There is **no `NEEDS_REVIEW`, `STOPPED`, `BLOCKED` or `ON_HOLD`**. A job is
stale when it is unfinished and nothing will move it, so every unfinished state
carries a deadline:

| State | Deadline | When it passes |
|---|---|---|
| ACCEPTED | picked up within N minutes | the worker picks it up (oldest first) |
| RUNNING | the lease | another worker continues it **at the saved activity** |
| WAITING | its defined wait (section 4) | progress is checked; no progress by the limit → **fired** |
| WAITING on a service | 72 h (the existing service give-up) | `FAILED`, naming the service; cleanup runs first |

* **Progress is saved after every activity.** A crash, restart or watchdog
  kill continues at the activity that was running.
* **Activities check what already happened before acting.** Submitting looks
  for the torrent by hash before adding it; Downloading checks progress instead
  of starting over. Doing an activity again is always safe.
* **Errors spend a retry budget, outages don't.** An unexpected error fails
  *that job* with its failed check. It never stops the worker (#7362).
* **Failing includes cleaning up.** Before `FAILED` or `CANCELED`, the job
  runs *Cleaning up*: it releases its reservation and removes its torrents and
  staged files. Nothing is left held.

## 3. Nothing needs review

Today about 150 places in 33 modules send work to Needs review, for about 40
reasons. On `refactor`, each one becomes a wait, a rejected release, a plain
cleanup or a bug report, and **the work keeps moving**:

| Today | Group | On `refactor` |
|---|---|---|
| `SERVICE_SETUP_INVALID`, `SERVICE_RESPONSE_INVALID` | service setup | **wait** for the service (one service alert, not N stuck jobs) |
| size limit, missing audio, `RELEASE_CONFLICT`, `TORRENT_ALREADY_PRESENT`, identity | unusable release | **reject that release** and try the next candidate; the episode keeps going |
| `EPISODE_RESERVATION_CONFLICT`, plan conflicts, `TORRENT_OWNERSHIP_CONFLICT`, `TORRENT_TRANSFER_CONFLICT` | our own jobs clashing | **wait** for the owning job, or end as a duplicate |
| `CANCELLATION_REVIEW`, `DECISION_DOWNLOAD_CLEANUP_REVIEW`, `POST_KEEP_CLEANUP_REVIEW`, `REJECT_CLEANUP_REVIEW`, `KEEP_IMPORT_REVIEW`, `STAGING_REVIEW` | cleanup "ownership proof" | **removed.** The `resources` interface cleans up what it recorded (section 5). Already gone counts as done |
| `MEDIA_SERVER_REFRESH_REVIEW` | Jellyfin | retry later; never blocks |
| `JOB_PAYLOAD_INVALID`, `JOB_STATE_INVALID`, `UNSUPPORTED_JOB_KIND` | our bugs | a **bug report** with the failed check; the episode goes back to searching |

* **Bug reports** are a list you check when you want. They never hold up a job.
* The full list of today's review paths, each mapped to its row above, is
  part of phase JD-0. Ranking by real counts from the server comes first.
* The dashboard shows what matters: **episodes moving** (doing what), **episodes
  waiting** (why, until when) and **episodes done** today.

## 4. Defined waits, supervision and firing

### Every wait is defined

A wait is not "check again later". Each waiting activity declares **what
progress looks like** and **how long it may go without any**:

| Activity | Progress means | No-progress limit |
|---|---|---|
| Downloading | bytes downloaded went up, or the torrent completed | 30 min (configurable) |
| Waiting for metadata | qBittorrent has the file list | 15 min |
| Waiting for a service | the service is healthy again (service health layer) | 72 h |
| Waiting for your evaluation | you decided Keep or Reject | none: it is your decision and shown as such |

### Following downloads without stressing qBittorrent

Jobs **do not call qBittorrent one by one**. One shared poller asks
qBittorrent about *all* our torrents in **one API call**
(`/api/v2/sync/maindata`, incremental: only what changed since the last call)
every N seconds (default 15, configurable). It writes each torrent's progress
to the database. Downloading activities read that snapshot.

* One call every 15 s, whether 1 or 200 torrents are downloading.
* While nothing is downloading, the poller does not poll at all.
* qBittorrent outages go through the service health layer: one wait, not N
  failing jobs.

### The worker supervises every job

Like PHP's `max_execution_time`, but for progress: the worker asks every
unfinished job "what have you done since my last check?"

* Each job records **its last progress**: what changed and when. Examples are an
  activity finished, more bytes downloaded, or a service came back.
* When the worker checks a job, it can find:
  * **progress since the last check:** fine, carry on;
  * **no progress, and within its activity's limit:** fine, it is waiting as defined;
  * **no progress past its activity's limit:** **fired**. The worker stops the
    job, runs *Cleaning up* (section 5) and ends it as `FIRED`, naming the
    activity, what it was waiting for and since when.
* After a fired **episode** job, the episode is not abandoned. The worker
  starts the next candidate release for that episode, if there is one. A fired
  job never just sits there.
* **There are no repeats.** A job doesn't loop on a check: it either makes
  progress, waits within its defined limit, or is fired. The History shows one line
  per change, never the same check again and again.
* A running activity that hangs (no return, lease still held) is covered too:
  its lease expires, and if no progress was recorded, the job is fired instead
  of being started again.

## 5. Files through `resources`, torrents through client adapters

### Today

**17 modules** have their own staging and cleanup logic (`cancel_cleanup`,
`decision_download_cleanup`, `post_keep_cleanup`, `batch_decision_cleanup`,
`keep_cleanup_proof`, `staging_artifacts`, …). **21 tables** record created
things, kept separately for single episodes and batches:
- `staging_publications` / `batch_staging_publications`;
- `keep_imports` / `batch_keep_imports`;
- `cancellation_cleanup` / `batch_cancellation_cleanup`;
- `batch_download_cleanup`, `batch_keep_cleanup`, `torrent_submissions`,
  `torrent_transfers`, …

There are also **three qBittorrent clients**:
- `acquisition/qbittorrent.py` (single episodes);
- `batch/qb_client.py` (batches);
- `services/adapters/qbittorrent.py` (health).

Each has its own version handling.

### Torrent client adapters

qBittorrent's Web API differs between the versions we support (4.5.2 up to
5.2). Before WebAPI 2.11 a torrent is *paused* and *resumed*; from 2.11 it is
*stopped* and *started*. Metadata-only fetch exists only from 5.2. Each
version range gets an **adapter**, and every adapter fulfils **one contract**:

```python
class TorrentClient:                       # services/adapters/torrent_client.py
    def add(self, source, save_path, *, stopped=True) -> Torrent
    def select_files(self, hash, indexes)
    def start(self, hash)                  # "resume" before WebAPI 2.11
    def stop(self, hash)                   # "pause" before WebAPI 2.11
    def remove(self, hash, *, with_data)
    def fetch_metadata(self, source)       # 5.2+; older adapters return None: use add+stop
    def files(self, hash) -> list[TorrentFile]   # same shape for every version
    def changes(self, since) -> Changes    # one incremental sync/maindata call for all torrents
```

* **Adapters never touch files.** They only talk to qBittorrent and adapt to
  its version. qBittorrent itself writes the download, and the adapter reports
  it.
* **Every adapter reports the files a torrent uses in the same format:**
  `TorrentFile(index, path, size, progress, selected)`, with `path` absolute
  (save path + content path), whatever the version returns.
* The adapter is picked once at connect time from `app/version` and
  `app/webapiVersion`. One client replaces the three above, and the health
  check uses the same adapter.
* The shared poller (section 4) is `changes()`: one call for all torrents.
* Adding qBittorrent 5.3, or another client later, means adding one adapter.
  No job changes.

### `resources`: one interface for files

Every job goes through **one interface**, `resources.py`, for anything it does to a
file or folder, and to record the files a torrent uses:

```python
res = resources.for_job(job)
torrent = client.add(magnet, save_path)               # adapter: talks to qBittorrent only
res.track_torrent(torrent, client.files(torrent.hash))  # recorded: hash + every file it uses
staged  = res.link(video, staging_path)                 # recorded: hardlink staging → download
nfo     = res.write_file(staging_dir / "S01E03.nfo", data)
kept    = res.import_to_library(staged, library_path)   # recorded: library copy, yours now
res.cleanup(client)                                     # undo what this job did (rules below)
```

* **It records everything as it happens.** There is one table, `resources`
  (what: file, folder, torrent or library import; path or hash; size; which
  job; which resource it came from; current state). A second table,
  `resource_operations`, holds every operation on it (created, linked,
  moved, imported, deleted), with time and job.
* **Torrent files are tracked uniformly.** A torrent is recorded with the files
  its adapter reported, so every download path is known the same way for every
  qBittorrent version.
* **It shows the work.** The job's report and the dashboard list, per file,
  what was done to it: *downloaded → linked to staging → imported to library →
  download removed*.
* **It cleans up.** `cleanup()` walks the job's own records in reverse.
  Torrents are removed through the adapter (`remove(with_data=True)`), and the
  tracked files are then confirmed gone. Files and folders are deleted
  directly. The rules are the same for every duty, episode or batch:

  | Job ending | Torrent and download | Staging files | Library import |
  |---|---|---|---|
  | DONE, kept | removed (with data) | removed | **kept** (it's yours) |
  | DONE, you rejected it | removed (with data) | removed | n/a |
  | REJECTED, FAILED, FIRED, CANCELED | removed (with data) | removed | n/a |
  | Reset of an episode | removed (with data) | removed | n/a, then search again |

* **Already gone counts as done.** A file someone deleted by hand, or a
  torrent removed in qBittorrent, is not an error.
* **One guard stays:** `resources` only creates or deletes inside the
  configured download, staging and library folders. A path outside them is a
  bug report, and nothing is touched.
* **Only `resources` touches the disk, and only the adapters talk to
  qBittorrent.** A test scans the code: no other module may call `os.unlink`,
  `os.link`, `shutil` or `mkdir`, or make qBittorrent requests. A new job can't
  quietly invent its own file handling or cleanup.
* **Shared torrents.** A second job that needs the same torrent (e.g. a
  season pack already downloading) is recorded as another *user* of that
  resource. Cleanup removes it only when its last user is done.

### What happens to the old modules and tables

* The 17 modules' logic becomes the rules above, and their 21 tables migrate into
  `resources` / `resource_operations` (one migration, history included, as
  with the earlier renames).
* The three qBittorrent clients become the adapters.
* The old modules, tables and clients are removed, with ledger entries in
  `removed.json`.

## 6. Quiet, level-respecting logging

Measured today (local, a real worker attempt):
- WARNING logged 0 lines and DEBUG logged 4. So the level *is* applied to job
  loggers.
- DEBUG adds nothing, because the code has almost no DEBUG messages, so it looks
  ignored.
- The worker's own logger reads the level **only at startup**.
- `logging.components` silently overrides `logging.level`.
- Every attempt writes **4 History rows** to the database, even when nothing changed.

On `refactor`:

* **History records changes only:** accepted, activity started, activity
  finished, state changed, waiting reason changed, ended. A repeat that changed
  nothing writes nothing.
* **Log lines follow the same rule.** INFO shows lifecycle changes; DEBUG
  shows each check (the repeats); WARNING shows failures and service problems.
* **The level applies live** to every logger (worker, job, API, providers)
  on the next operation, without a restart. The settings page shows the
  effective level per component, so an override is visible.
* A test asserts the History and log volume of one download: a fixed small number
  of rows, not one per poll.

### The log line (agreed 2026-11-08, built in JD-1d)

`time LEVEL job #N [subject] · activity · what happened · facts`

```
2026-11-08 18:40:02 INFO    job #900 Frieren S01E03 · Identifying · done · AniDB 17617, TVDB 424536 S01E03
2026-11-08 18:40:04 DEBUG   job #900 Frieren S01E03 · Verifying release · check EPISODE_SIZE passed · settings.py:93 · 1434 MB (expected ≤ 2000 MB)
2026-11-08 19:05:10 WARNING job #901 Frieren S01E04 · Verifying release · check EPISODE_SIZE failed · settings.py:93 · 2650 MB (expected ≤ 2000 MB)
```

* **One job, one name.** No `event_id`, `created_by_job_id`, `trace_id`,
  `invocation_id` or `component` fields. A non-worker emitter is noted as
  "from api".
* **The catalog's words are the message.** Codes stay in History.
* **Checks always name themselves** (`checks.check` / `require` / `here`): the
  name, the real file:line, and on failure actual vs expected and the path.
* **INFO is lifecycle only.** Claiming, handler start/end and repeated waits are
  DEBUG. Every attempt that does not complete logs its failed check at DEBUG.

## Phases (each a PR, tests local only)

0. **JD-0 Measure and map.** Rank today's review, hold and fail reasons with
   counts from the server; map every review path to section 3; fix the logging
   level bugs (startup-only level, override visibility). Bug fixes go to NextGen too.
1. **JD-1 `release_names.py`, torrent adapters, `resources.py` and activities.**
   - Parsing in one module.
   - The three qBittorrent clients become version adapters behind one contract.
   - The resources interface, with today's file operations moved behind it, and
     the scan test.
   - Each handler becomes an activity function.

   No behaviour change yet, except that every file and torrent is now recorded.
2. **JD-2 Episode duty.** Job created on accept, Identifying as its first
   activity, `REJECTED` as a normal ending, saved progress, deadlines,
   repeat counting and change-only History. Test: one release end to end as
   **one job**, a restart mid-download, a rejected re-send, and the History
   volume of one download.
3. **JD-3 No review; supervision.** Each review path becomes a wait, a rejected
   release (with the next candidate), a plain cleanup or a bug report.
   Defined waits, progress records, firing, and the shared qBittorrent poller.
4. **JD-4 Season-pack and batch duties.**
5. **JD-5 Reset and cleanup through `resources`.** The cleanup rules table
   replaces the 17 cleanup modules; the 21 tables migrate into `resources`. No
   cross-job waiting.
6. **JD-6 Switch-over, dashboard and report**: episodes moving, waiting and
   done; bug reports. The old kinds and review states are removed
   (ledger entries).

## Decisions for the owner

1. **In-flight work at switch-over.** Recommended: old job chains finish
   the old way, and new work starts as duty jobs.
2. **Duty rules.** Recommended: today's rules for episode, season pack and
   batch, moved into the worker, with no new rules yet.
3. **No-progress limits.** Recommended: downloads 30 min without new bytes,
   metadata 15 min, service waits 72 h; all configurable.
4. **qBittorrent poll interval.** Recommended: 15 s, one call for all torrents.
