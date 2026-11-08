# Series job: one job per series does all of that series' work

Status: **design, awaiting owner approval**. No code changes yet.
Branch: `refactor`. The owner's rule is that NextGen keeps only fixes, and behaviour
changes that improve things land on `refactor` and are listed in the PR.

## What the owner wants

> The worker accepts the work and decides what it is: an episode, a season
> pack or a batch. It gives the work to the series' job. A second release of
> the same series attaches to the same job. Don't launch new jobs per step.
> The worker's concurrency handles waiting.

## Why

Today one release becomes a chain of a dozen or more jobs:
- Identify, Verify, Reserve, Prepare, Submit and Download;
- Verify media, Stage and Keep;
- Cleanup and Reset.

Each job knows only the job that created it, and batches have their own
parallel chain of nine kinds. What the work *is* (an episode or a pack) is
discovered partway down the chain. When something stops, you have to find which
of the many jobs stopped and why. Jobs that coordinate other jobs (a reset waiting
on the cleanups it queued) have to rediscover them from the database. Job #7362
got stuck that way.

## The model

### The worker accepts and decides

The worker is the single entry point for work. For every webhook, manual
release, retry, reset or Keep/Reject decision, it:

1. **Accepts** the incoming work (today's intake and identification: which
   series is this?).
2. **Classifies** it: `EPISODE` (one episode, e.g. S01E01), `SEASON_PACK`
   (a whole season in one torrent) or `BATCH` (several episodes or seasons).
   Today this decision is spread across verify, discovery and batch evaluation.
   It moves to the worker and happens once, up front.
3. **Attaches** it to the series' job as a work item. It creates that job the
   first time a series is seen. A second release of the same series goes to the same job.
4. **Runs** the item.

### One job per series

* The series job holds all the work for that series: every episode, season
  pack and batch item, whatever its state.
* It **does the work itself**, step by step, with no new jobs launched per step.
  The steps are today's job kinds, reused as stages:

  ```
  EPISODE:     verify release → select → reserve → prepare → submit → download
               → verify media → stage → (user evaluates) → keep | reject → cleanup
  SEASON_PACK / BATCH:
               evaluate → reserve → submit → transfer → verify probe
               → verify episodes → stage → keep → unlock → cleanup
  ```

  The stage code is today's handler code: the same checks and the same atomic
  commits. Only the caller changes, from a separate job to the series job.
* Each item's current stage is saved after every step. If the worker
  restarts, every item continues from its saved stage.

### Concurrency

* The worker runs items **concurrently**: several episodes of one series, and
  many series, at the same time, within the worker's concurrency limit.
* An item that is waiting (a download in progress, a service outage) does
  not block anything. The other items and series keep running while it waits.
* Two items of the same series that would touch the same thing, such as the
  same episode or the same torrent, are serialised by the series job. This
  replaces today's cross-job reservation conflicts within a series.

### Items are isolated

An item that stops for review (a cleanup that found a file it doesn't own, or a
size limit) stops *only that item*. The other episodes keep going. An
unexpected error in one item sends that item to review; it does not stop the
job or the worker.

### The series view

```
Series job #812 · Frieren (AniDB 17617)          1 item needs review
  S01E01          kept
  S01E03          NEEDS REVIEW  cleanup · cancellation_service.py:… TORRENT_MOVED
                  file /downloads/…                    [Resume] [Reset]
  S01E04          downloading 42%
  Season pack S02 (12 episodes)  verifying episode 7 of 12
```

## What stays outside the series job

* **System jobs** that aren't about one series: Jellyfin refresh, bulk
  management actions and service health. These stay as they are.
* **Releases the worker can't identify** are not attached to any series. They
  wait as "unidentified" (with today's manual-identity action) until a series is known.

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
-- watchdog, history and the dashboard plumbing. One per anime. The worker
-- runs its items; each item carries its own stage and state.
CREATE UNIQUE INDEX jobs_one_series ON jobs(json_extract(payload_json,'$.anidb_id'))
  WHERE kind='SERIES';

CREATE TABLE series_work_items(
  item_id        INTEGER PRIMARY KEY,
  series_job_id  INTEGER NOT NULL REFERENCES jobs(job_id),
  scope          TEXT NOT NULL CHECK(scope IN ('EPISODE','SEASON_PACK','BATCH')),  -- decided by the worker
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
* The worker picks up items by `state` and `next_check_at_ms`, so a waiting
  item costs nothing until its next check.

## Phases (each a PR, tests local only)

1. **SJ-1 Stage interface.** Turn each handler's work into a stage function
   that both today's job wrapper and the series job can call. No behaviour
   change: every job still runs as today. The large mechanical part is done
   safely.
2. **SJ-2 Worker accepts and classifies; series job with episode items.**
   The worker classifies each release once and attaches it to the series job.
   This phase adds the `SERIES` kind, work items, concurrent items and
   per-item isolation. Episode releases route to it.
   Test: one release end to end in a real worker, two episodes of one series
   at once, and one item stopping while the other finishes.
3. **SJ-3 Season-pack and batch items.**
4. **SJ-4 Reset, cleanup, cancel and resume as item actions.**
5. **SJ-5 Switch-over.** Intake sends all new work to series jobs. Old job
   chains still in progress are handled per the owner's decision below.
6. **SJ-6 Dashboard and report.** The series view above and
   `adm report series --anidb N`. The old per-stage kinds are removed (ledger
   entries in `removed.json`).

## Trade-offs to know

* **Concurrency limit.** Items share the worker's concurrency limit. One
  large series (a 12-episode pack plus singles) can use several slots; the
  limit stays configurable.
* **Reservations get simpler.** Episodes of one series no longer compete
  across separate jobs, because one job owns them all.
* **This is a large rework of orchestration.** SJ-1 keeps it safe: the
  stage code is shared and tested under today's jobs before anything switches.

## Decisions for the owner

1. **In-flight work at switch-over.** Recommended: let old job chains finish
   with the old code, and start only new work as series jobs (a drain period
   of hours to days). Alternative: convert each in-flight chain into a work
   item at its current stage, using a migration.
2. **Classification rules.** Recommended: the worker uses today's rules
   (release verification and batch evaluation) to decide episode, season pack
   or batch, moved to one place, with no new rules yet.
3. **Job numbers.** Recommended: the series job has one number, and items show
   as `S01E03` and `Batch S02` (no per-step job numbers any more). Old job
   numbers stay in History.
4. **Resume scope.** Recommended: Resume on one item. The series view also
   offers "Resume all stopped items".
