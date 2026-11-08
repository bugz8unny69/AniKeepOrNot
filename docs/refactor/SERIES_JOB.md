# Series job: one job per series does all of that series' work

Status: **design, awaiting owner approval**. No code changes yet.
Branch: `refactor`. The owner's rule is that NextGen keeps only fixes, and behaviour
changes that improve things land on `refactor` and are listed in the PR.

## What the owner wants

> One job handles a series. It can be awake or asleep. When a new episode
> arrives, the same job wakes and handles it. Don't launch new jobs: one job
> does episodes, seasons and batches.

## Why

Today one release becomes a chain of a dozen or more jobs:
- Identify, Verify, Reserve, Prepare, Submit and Download;
- Verify media, Stage and Keep;
- Cleanup and Reset.

Each job knows only the job that created it. Batches have their own parallel chain of nine kinds. When something stops, you have to find which of the many
jobs stopped and why. Jobs that coordinate other jobs (a reset waiting on the cleanups it
queued) have to rediscover them from the database. Job #7362 got stuck that way.

## The model

**One series job per series (AniDB anime), for its whole life.**

* **It sleeps when idle.** No work: it is not claimed and costs nothing.
* **New work wakes it.** A webhook or manual release for the series, a
  retry, a reset or a Keep/Reject decision adds a **work item** to the series
  job and wakes it.
* **Work items, not new jobs.** Each piece of work is an item inside the
  series job:
  * `EPISODE`: one episode (S01E01). This is today's single-release chain.
  * `BATCH`: one season pack or batch torrent, covering several episodes.
  * `SEASON`: "get this season". It decides between episode items and a
    batch, then becomes those.
* **Each item moves through stages.** The stages are today's job kinds,
  which become steps inside the item:

  ```
  EPISODE: verify release → select → reserve → prepare → submit → download
           → verify media → stage → (user evaluates) → keep | reject → cleanup
  BATCH:   discover → evaluate → reserve → submit → transfer → verify probe
           → verify episodes → stage → keep → unlock → cleanup
  ```

  The stage code is today's handler code: the same checks and the same atomic
  commits. Only the caller changes, from a separate job to the series job.
* **One pass at a time.** When awake, the series job:
  1. takes its lease;
  2. advances every item that is ready by one stage, committing each item
     separately under the lease;
  3. then sleeps until the earliest time an item needs checking (for example
     the next download progress check), or until woken.

  Waiting stages, such as downloads, metadata and service outages, are checks
  rather than blocking work. So one series can have several episodes
  downloading at once.
* **Items are isolated.** An item that stops for review (a cleanup that
  found a file it doesn't own, or a size limit) stops *only that item*. The
  other episodes keep going. An unexpected error in one item sends that item
  to review; it does not stop the job or the worker.
* **The series job always shows where everything is:**
  ```
  Series job #812 · Frieren (AniDB 17617)        awake · 1 item needs review
    S01E01  kept
    S01E03  NEEDS REVIEW  cleanup · cancellation_service.py:… TORRENT_MOVED
            file /downloads/…                    [Resume] [Reset]
    S01E04  downloading 42% · next check 19:02
    Batch S02 (12 episodes)  verifying episode 7 of 12
  ```

Different series still run in parallel: each series is its own job.

## What stays outside the series job

* **Identification.** Before a release is identified there is no series yet.
  Identification stays a small intake step per incoming release. When it
  finishes, it adds the work item to the series job (creating that job the
  first time) and wakes it.
* **System jobs** that aren't about one series: Jellyfin refresh, bulk
  management actions and service health. These stay as they are.

## Reset, cleanup, cancel and retry become item actions

* **Reset episode:** the item cleans up its own resources (same cleanup
  code, same ownership checks) and then goes back to its search stage. No
  separate reset and cleanup jobs, and no cross-job waiting. The #7362 problem
  can't happen.
* **Resume:** resumes the stopped item from the stage that stopped,
  recording who asked. A cleanup that stopped to protect files it doesn't own
  is resumed only by an explicit Resume, never automatically.
* **Cancel:** cancels the item (or all items) through the existing
  cancellation and cleanup path.

## Data model (sketch)

```sql
-- The series job is a row in `jobs` (kind SERIES): it reuses leases, the
-- watchdog, history and the dashboard plumbing. One per anime.
CREATE UNIQUE INDEX jobs_one_series ON jobs(json_extract(payload_json,'$.anidb_id'))
  WHERE kind='SERIES';

CREATE TABLE series_work_items(
  item_id        INTEGER PRIMARY KEY,
  series_job_id  INTEGER NOT NULL REFERENCES jobs(job_id),
  scope          TEXT NOT NULL CHECK(scope IN ('EPISODE','BATCH','SEASON')),
  event_id       INTEGER REFERENCES incoming_events(event_id),  -- the release that started it
  tvdb_season    INTEGER, tvdb_episode INTEGER,                 -- EPISODE / SEASON target
  stage          TEXT NOT NULL,                                 -- e.g. DOWNLOAD
  state          TEXT NOT NULL CHECK(state IN ('READY','WAITING','NEEDS_REVIEW','DONE','CANCELED')),
  reason_code    TEXT,
  next_check_at_ms INTEGER,
  attempt_count  INTEGER NOT NULL DEFAULT 0,
  created_at_ms  INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
);
```

* The existing per-stage tables (reservations, plans, submissions,
  transfers, publications and so on) stay. Today they are keyed by job or
  event; they gain `item_id` where needed.
* History is written on the series job, with `item_id` and `stage` in the
  details. One timeline per series, filterable per item.
* A series job sleeps by setting its next run to the earliest
  `next_check_at_ms`. With no waiting items, it has no next run until woken.

## Phases (each a PR, tests local only)

1. **SJ-1 Stage interface.** Turn each handler's work into a stage function
   that both today's job wrapper and the series job can call. No behaviour
   change: every job still runs as today. The large mechanical part is done
   safely.
2. **SJ-2 Series job and episode items.** The `SERIES` kind, work items,
   pass, sleep and wake, and per-item isolation. Episode releases route to it.
   Test: one release end to end in a real worker, two episodes of one series
   at once, and one item stopping while the other finishes.
3. **SJ-3 Batch and season items.**
4. **SJ-4 Reset, cleanup, cancel and resume as item actions.**
5. **SJ-5 Switch-over.** Intake sends all new work to series jobs. Old job
   chains still in progress are handled per the owner's decision below.
6. **SJ-6 Dashboard and report.** The series view above and
   `adm report series --anidb N`. The old per-stage kinds are removed (ledger
   entries in `removed.json`).

## Trade-offs to know

* **One pass at a time per series.** A slow stage (ffprobe of a large file, a
  file copy) delays that series' other items until it finishes. Other series
  are unaffected. Mitigation: bound each pass. Long file work already runs as
  bounded checks; anything that can't be bounded stays a blocking stage, and
  its duration is visible on the series job.
* **Reservations get simpler.** Episodes of one series no longer compete
  across separate jobs, because one job owns them all.
* **This is a large rework of orchestration.** SJ-1 keeps it safe: the
  stage code is shared and tested under today's jobs before anything switches.

## Decisions for the owner

1. **In-flight work at switch-over.** Recommended: let old job chains finish
   with the old code, and start only new work as series jobs (a drain period
   of hours to days). Alternative: convert each in-flight chain into a work
   item at its current stage, using a migration.
2. **Identification.** Recommended: it stays a small intake step per release
   (no series exists yet). Alternative: an "unidentified" holding job.
3. **Job numbers.** Recommended: the series job has one number, and items show
   as `S01E03` and `Batch S02` (no per-step job numbers any more). Old job
   numbers stay in History.
4. **Resume scope.** Recommended: Resume on one item. The series view also
   offers "Resume all stopped items".
