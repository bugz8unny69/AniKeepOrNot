# Manual checkpoint Retry (NextGen)

A dashboard **Retry** button is shown for eligible acquisition jobs stopped in
`NEEDS_REVIEW`. It uses the authenticated, CSRF-protected management endpoint:

```http
POST /api/v1/jobs/{job_id}/retry
Content-Type: application/json

{"expected_attempt":4,"expected_reason":"JOB_RETRY_EXHAUSTED"}
```

The values come from the latest GET job snapshot (`attempt_count` and
`reason.code`). The response is a **queued** receipt, never a completion
promise. Repeated identical requests still pending are idempotent; stale clicks
return 409. No webhook resend, new acquisition or unrelated job is created.

## Existing infrastructure, unchanged

- Requeue the **same** stopped job in `PENDING`, preserving its payload,
  predecessors, attempt count, operation, evidence, resources, and history.
- The existing Worker claims it normally, with fresh configuration, a new
  lease and the same handler kind. Its persisted submission, transfer, media,
  and staging journals remain authoritative for reconciling earlier effects.
- The `RETRY_REQUESTED` audit marks a new **automatic failure-budget window**.
  Only `RETRY_SCHEDULED` events *after* that marker count against the new
  window. Old failures and attempt numbers are never erased.
- Explicit allowlist covers single-episode and batch acquisition jobs through
  individual staging steps. Specialized Keep, Reject, cancellation, cleanup and
  post-Keep follow-up resumes keep their existing management endpoints.
- No generic Retry for running/leased, completed, canceled, inactive-operation,
  or already-handed-off jobs. No manual Retry for `ON_HOLD` condition waits or
  ordinary `RETRY_WAIT` jobs; the worker already handles those.

Retry is **not a force operation**: unresolved torrent ownership, uncertain
one-shot network commands, changed files, missing/unsafe staging receipts, and
other handler-specific review conditions remain blocked by their existing
safety checks. The attempt may return to `NEEDS_REVIEW` if the underlying
problem is not fixed. Operators must correct the cause first.

No schema migration, new scheduler, new workflow table, new external I/O or
automatic replay of HTTP actions is introduced.

Offline storage/API/browser tests are included. They have not been executed
against a running application, qBittorrent, Sonarr or a live filesystem.
