# Jobs dashboard: manual retry and cancellation

The master service remains the only systemd service. Its child worker uses
SQLite leases and a bounded exponential retry delay (up to 60 minutes).
Operators can now act on waiting jobs from **AniKeepOrNot → Jobs**:

- **Retry Now**: available for `DEFERRED` acquisition and one-time search
  jobs. Moves the job back to `READY` immediately; the master worker picks
  it up during its ordinary polling loop. Does not reset attempts, stored
  error, authorization evidence, payload or qBittorrent submission records.
- **Cancel Job**: available for `READY`, `DEFERRED`, or `NEEDS_REVIEW`
  jobs. Stops future scheduler claims and displays `CANCELED`. It does
  not interrupt an executing worker or remove any torrent or media.
  Canceled jobs are terminal unless changed by a future explicit,
  operator-supported workflow.
- **Details**: each job includes its internal ID, scheduler state, attempt
  count, last exception type, next retry time when deferred, current lease
  expiry when active, and last admin action.

The dashboard shows separate IDs for incoming notifications and internal
worker jobs. The operations use the **internal job ID**. Buttons are
never offered for already completed, actively leased, canceled, or
naturally blocked jobs. A just-expired lease is not manually preempted;
the worker scheduler already owns lease recovery.

## Safety model

The API `POST /api/manage-job` requires a valid administrator session
and session-bound CSRF token, like the other dashboard mutations.
Requests carry the job ID, action (`retry` or `cancel`), expected
state, and expected attempt count. Under a SQLite `BEGIN IMMEDIATE`
transaction the backend checks the current state and attempts and
performs a conditional update. Stale UI requests and double-clicks fail
with HTTP 409; refresh the dashboard instead of forcing the change.

The audit table `daemon_job_admin_events` stores each successful action,
the previous scheduler state, attempt count, last error and timestamp.
To preserve compatibility with the existing SQLite CHECK constraint,
the underlying canceled job uses `BLOCKED` and the last cancel audit event
makes the dashboard display `CANCELED`. There is no table-copy migration.

Do not manually edit the SQLite database to clear errors or fabricate
new jobs. A deferred acquisition failure such as `IdentityNotReady`
may reoccur after manual retry; in that case the diagnostic fields update
and the scheduler resumes backoff. Retry is not a substitute for resolving
missing identity evidence.
