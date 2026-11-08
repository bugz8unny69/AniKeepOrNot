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
6. **Cleanup deletes what we created.** The database records every path and
   hash we created, so cleanup removes exactly those. No extra "prove we own
   it" check, no leftovers.
7. **Waits are defined and gentle.** A download is followed in qBittorrent
   while it downloads, without stressing qBittorrent's API.
5. **Logging is cheap and respects the configured level.** Writing a log or
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
| `CANCELLATION_REVIEW`, `DECISION_DOWNLOAD_CLEANUP_REVIEW`, `POST_KEEP_CLEANUP_REVIEW`, `REJECT_CLEANUP_REVIEW`, `KEEP_IMPORT_REVIEW`, `STAGING_REVIEW` | cleanup "ownership proof" | **removed.** Cleanup deletes the paths and torrent hashes the database recorded when we created them (section 5). Already gone counts as done |
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

## 5. Cleanup deletes what we created

Every file, folder and torrent a job creates is recorded **when it is
created**: the path and size of each staged or downloaded file, and the torrent
hash with its save path. Cleanup removes exactly those records:

* a torrent: removed from qBittorrent by its recorded hash, with its data;
* a file or folder: deleted at its recorded path;
* already gone: counts as cleaned up, not as an error.

The "can we prove we own it" checks are removed. These are the inode/size
re-verification, torrent-moved detection and unowned-file review that today end
in `CANCELLATION_REVIEW` and similar. One guard stays: cleanup only deletes
inside the configured download, staging and library folders. A recorded path
outside them is a bug report, not a deletion.

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

## Phases (each a PR, tests local only)

0. **JD-0 Measure and map.** Rank today's review, hold and fail reasons with
   counts from the server; map every review path to section 3; fix the logging
   level bugs (startup-only level, override visibility). Bug fixes go to NextGen too.
1. **JD-1 `release_names.py` and activities.** Parsing in one module; each
   handler becomes an activity function. No behaviour change yet.
2. **JD-2 Episode duty.** Job created on accept, Identifying as its first
   activity, `REJECTED` as a normal ending, saved progress, deadlines,
   repeat counting and change-only History. Test: one release end to end as
   **one job**, a restart mid-download, a rejected re-send, and the History
   volume of one download.
3. **JD-3 No review; supervision.** Each review path becomes a wait, a rejected
   release (with the next candidate), a plain cleanup or a bug report.
   Defined waits, progress records, firing, and the shared qBittorrent poller.
4. **JD-4 Season-pack and batch duties.**
5. **JD-5 Reset and cleanup duties**, with no cross-job waiting.
6. **JD-6 Switch-over, dashboard and report**: episodes moving, waiting and
   done; Leftovers; bug reports. The old kinds and review states are removed
   (ledger entries).

## Decisions for the owner

1. **In-flight work at switch-over.** Recommended: old job chains finish
   the old way, and new work starts as duty jobs.
2. **Duty rules.** Recommended: today's rules for episode, season pack and
   batch, moved into the worker, with no new rules yet.
3. **No-progress limits.** Recommended: downloads 30 min without new bytes,
   metadata 15 min, service waits 72 h; all configurable.
4. **qBittorrent poll interval.** Recommended: 15 s, one call for all torrents.
