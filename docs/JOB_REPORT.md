# Job report diagnostics

```
sudo adm report job --id 1292            # text
sudo adm report job --id 1292 --json     # structured diagnostics only
sudo adm report --compress job --id 1292 # /tmp/job-report-1292.tar.gz
```

The report is read-only. It opens the database with `mode=ro` and
`query_only`. It never retries, cancels, releases or cleans anything up, and it
makes no qBittorrent, Sonarr or Prowlarr calls. The only live observation is
the existing on-disk existence check of recorded paths, which is labeled as one.

## Sections

| Section | Answers |
|---|---|
| Job summary | ID, kind, state, attempts, reason and advice (reason registry), release, trace, payload, creator |
| Current state | What the job is doing, what it waits for, the condition to continue, whether a **documented** automatic path exists, whether a person must act, last successful step, last recorded activity, and a *Suspected stranded* flag |
| Blocking dependencies | The **creator** (who made this job) apart from the **blockers** (what stops it): reservation owners, management targets, downloads/searches, cancellation. A bounded chain (6 hops, cycle-safe) follows the blocker's own stuck job. |
| Episode reservation | `RESERVE_EPISODE` only: episode identity, this job's own reservation, every matching single or batch reservation, owner job and operation, a validity verdict, released matches, and the wake checks `wake_waiters` would apply |
| Download and staging | Plan, info hash, torrent submission and save path, transfer phase and progress, media verification, staging publication; next step from recorded successors |
| Recovery and retry | Lease state, interrupted attempts (`HANDLER_STARTED` with no end), lease expirations reclaimed, retries used in the current window, manual retry requests, whether Retry is available, operation cleanup |
| Operation | The existing member list, plus jobs grouped as complete / executing / waiting / intervention / canceled, and whether any of them has a path to progress. `ACTIVE` is not treated as proof of work. |
| History | Unchanged order. Each event shows its reason (or its registered event message), the attempt, and the cause reason |
| Records and paths | Unchanged |

Wording: `Not recorded` = the database has no such fact. `Not checked` = the report
did not look (for example, live qBittorrent state). `MISSING` under *Paths on disk* = checked
live and confirmed absent.

## Reservation validity

The verdict follows the lifecycle rules in the code. It never says "orphaned" just because the owner is not running.

| Verdict | Meaning |
|---|---|
| VALID — owner in progress | Owner's operation has a queued, running or scheduled job. This job resumes automatically when the owner stages (closed as `SKIPPED_DUPLICATE`), or when the owner is canceled and cleaned up (woken). |
| VALID — owner stopped | The reservation stays active by rule, but the owner's work is in review, blocked or on hold. Fix that job or cancel the owner's operation. |
| VALID — suspected stranded owner | Owner's operation is `ACTIVE`, but every job finished, nothing was published and the last step created no successor. Nothing automatic will free the slot. |
| VALID — owner staged | Owner published the episode, so this job is a duplicate the close step missed. |
| UNCERTAIN | Owner job or operation is missing, or the cancellation completed without releasing the reservation (potentially orphaned). |

## Resume paths the report relies on

These are the only mechanisms in the code that move a held reservation job:

1. `skip_staged_waiters` (staging completion) closes held exact duplicates as `DONE`/`SKIPPED_DUPLICATE`.
2. `release_cancelled` → `wake_waiters` (owner `CANCELED` with cleanup `COMPLETE`) releases single-episode reservations. Held duplicates go to `PENDING` if their own operation is `ACTIVE` and their verified facts still check out.

Nothing releases a reservation on a timer, on restart, or when the owner finishes.
Retry (`review_retry`) only covers `NEEDS_REVIEW` (and a few `BLOCKED` search reasons), so it cannot resume an `ON_HOLD` job.

## Data-model limitations found

- The `ON_HOLD` history event records only the attempt, not which reservation blocked the job. The report works the blocker out from current reservations and says so.
- Batch reservations block single-episode jobs, but releasing a batch reservation never calls `wake_waiters`, and batch staging never closes held single-episode jobs. A job held by a batch reservation has no automatic resume path.
- There is no supported action that resumes an `ON_HOLD` job. Only waking, closing or canceling the job ends the hold.
- The worker's retry limit (`Policy.max_retries`) is not persisted. The report shows the default (3).
- Live qBittorrent state is not read. Torrent state comes from the persisted submission and transfer journals.
- `adm report` reads this job's own operation. The dependency chain follows blockers into other operations, but only the first stuck job at each hop.
