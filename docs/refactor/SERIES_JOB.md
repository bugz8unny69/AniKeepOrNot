# Series job: one job per series, steps do the work

Status: **design, awaiting owner approval**. No code changes yet.
Branch: `refactor` (the owner's rule: NextGen keeps only fixes; behaviour
changes that improve things land on `refactor` and are listed in the PR).

## Why

Today a series is spread across dozens of numbered jobs (Identify, Verify,
Reserve, Prepare, Submit, Download, Verify media, Stage, Keep, Cleanup, Reset
and so on). Each job only knows the job that created it. When something stops,
you have to find which job stopped and why, by hand.

Job #7362 showed the cost. The reset queued cleanup jobs without linking
them to itself. One cleanup stopped for review, and the reset kept refusing
without saying which job to look at. Retrying the reset could never help.
(#300 now names the job, but it still has to rediscover it from the database.)

## The model

**One series job per series (AniDB anime).** It is the single place that
shows everything happening for that series.

* **Steps do the work.** The existing job kinds stay as they are: same
  handlers, same atomic commits, same safety checks. Each step does its own
  piece and records its own outcome (done, waiting, stopped with its failed
  check and file).
* **The series job collects.** It never calls Prowlarr, qBittorrent, Sonarr
  or the filesystem. It holds the series' combined state, built from its steps:
  * `ACTIVE`: at least one step is pending or running. It shows the current
    step and what it is doing.
  * `WAITING`: no step can run right now (a service outage, a hold, a
    timer). It shows what it is waiting for and until when.
  * `NEEDS_REVIEW`: a step stopped. It shows *that step*, its failed check,
    its file and the action that resumes it.
  * `IDLE`: no unfinished steps. Nothing to do until new work arrives
    (a new release, a retry, a reset).
* **Collection is event-driven, not polling.** Whenever a step changes
  state, the worker updates its series job in the same transaction. The two
  can never disagree, and nothing has to re-derive state from other tables.
* **Steps can have sub-steps.** A reset links the cleanups it queues as its
  own children (`created_by_job_id` = the reset) and waits for *them*. It no
  longer re-discovers owners from the database on each attempt. A child that
  stops makes the reset stop, with the child's check, file and job number.

```
Series #812 · Frieren (AniDB 17617)          NEEDS_REVIEW
├─ Episode 3
│  ├─ #7340 Clean failed episode attempts …  waiting on #7351
│  │  └─ #7351 Clean up canceled work         STOPPED cancellation_service.py:… TORRENT_MOVED
│  │                                          file /downloads/…  [Resume]
│  └─ (new search starts when #7340 finishes)
└─ Episode 4
   └─ #7366 Download release                  ACTIVE 42%
```

## Retry and resume

* **Resume** on the series job resumes the stopped step(s) it shows. It
  is an explicit administrator action, recorded on both the step and the
  series job. It never deletes anything itself.
* A step that stopped to protect files it does not own (cleanup review) is
  never resumed automatically, not even by a reset.
* Retrying a parent step (e.g. a reset) re-collects its children's results;
  it does not redo their work.
* **Cancel** on the series job cancels all of its unfinished steps through the
  existing cancellation and cleanup path.

## Data model

New table, plus one column on `jobs`:

```sql
CREATE TABLE series_jobs(
  series_job_id   INTEGER PRIMARY KEY,
  anidb_id        INTEGER NOT NULL UNIQUE REFERENCES identified_anime(anidb_id),
  state           TEXT NOT NULL CHECK(state IN ('ACTIVE','WAITING','NEEDS_REVIEW','IDLE')),
  focus_job_id    INTEGER REFERENCES jobs(job_id),   -- the step it shows: running or stopped
  reason_code     TEXT,                              -- the focus step's reason
  waiting_until_ms INTEGER,
  created_at_ms   INTEGER NOT NULL,
  updated_at_ms   INTEGER NOT NULL
);
ALTER TABLE jobs ADD COLUMN series_job_id INTEGER REFERENCES series_jobs(series_job_id);
CREATE INDEX jobs_series ON jobs(series_job_id, state);
```

* `series_job_id` is **inherited**: `enqueue_job_with_receipt` copies it
  from the creating job. Identification sets it the first time, when the
  series is known.
* Before identification, a release has no series. Identify jobs, and anything
  that fails before identification, stay *unassigned* and appear under
  "Unidentified releases" (one entry per release event).
* Several releases of the same series (webhook, manual, batch, retry) all
  attach to the same series job.
* History: a series job's own events (`SERIES_JOB_STATE_CHANGED`,
  `SERIES_JOB_RESUME_REQUESTED`, …) go in `job_history` against the focus step,
  with `series_job_id` in the details. Nothing new is needed for the step histories.

**Migration** (next free number after the NextGen sync): create
`series_jobs` and backfill one row per series that has jobs. To do that, map each
job to its series through `identity_results(event_id → anidb_id)` and
the plan tables. Set `jobs.series_job_id`, then compute each series job's state from
its steps with the same function the worker uses. Rows that cannot be mapped
stay unassigned. Guard and foreign-key check follow the 058 pattern.

## Where it changes the code

| Area | Change |
|---|---|
| `storage/sqlite.py` `enqueue_job_with_receipt` | inherit `series_job_id`; identification assigns it |
| `storage/job_queue.py` (finish / defer / review / hold) | update the series job in the same transaction (`series_job.collect(db, series_job_id)`) |
| new `storage/series_job.py` | `collect()` (pure state derivation from steps), `resume()`, `cancel()` |
| `storage/episode_reset.py`, reset job | link its cleanups as children; wait on children instead of re-deriving owners |
| cleanup control jobs | `created_by_job_id` = the step that queued them (today: none) |
| API `/jobs` | default list = series jobs + unidentified releases; `/jobs/series/{id}` = the step tree |
| dashboard | Jobs page lists series; expanding shows steps; Resume/Cancel on the series |
| `adm report` | `adm report series --anidb N` (step tree + focus step's diagnostics) |
| `job_recovery`, batch audit | act per series job where they now act per step |

The handlers and their safety checks do not change. That is the point of
keeping steps as the existing job kinds.

## Phases (each a PR, tests local only)

1. **SJ-1 Schema and inheritance.** Migration, `series_job_id` inheritance,
   and the backfill. Test: every job created after identification has a series
   job, and a migrated real database maps every identified job.
2. **SJ-2 Collection.** `collect()` is called from every state change.
   Test: for each step outcome, the series job state and focus match
   (table-driven, plus a real-worker run of one release end to end).
3. **SJ-3 Parent steps wait on children.** The reset and its cleanups first,
   then the batch controls. Test: the #7362 case, where a stopped child stops the
   parent with the child's check and file.
4. **SJ-4 Dashboard and report.** Series list, step tree, Resume/Cancel,
   `adm report series`.
5. **SJ-5 Recovery tools.** `job_recovery` and the batch audit become
   series-aware.

## Decisions for the owner

1. **Lifetime.** Recommended: one series job per series, kept forever; it
   goes `IDLE` when nothing is left and becomes active again when new work arrives.
   Alternative: a new series job per release round.
2. **Step numbers.** Recommended: keep `Job #N` visible on steps. Reports,
   logs and History already use them.
3. **Unidentified releases.** Recommended: list them separately until
   identified (they have no series yet).
4. **Resume scope.** Recommended: Resume resumes every stopped step of the
   series it shows. Alternative: one step at a time.
