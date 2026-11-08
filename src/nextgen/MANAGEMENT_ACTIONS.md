Current Keep execution: KEEP_MEDIA now calls the Sonarr import service. Its
execution_available flag is true; staged eligibility and job-state explanations
still apply. Explicit Keep can resume the same stopped job. A completion message
comes from the durable import result. See [Keep/Sonarr](KEEP_SONARR.md). This
supersedes the older blocked-boundary notes below; old history remains readable.

# Durable dashboard and ADM actions

The API authenticates and records intent. The separate async Worker executes
installed `*_job.py` handlers. HTTP never keeps a request open while calling the
download client or deleting files. Webhook credentials cannot submit management
actions; browser sessions still require Origin and CSRF on mutations.

| Action | Admission and result | Worker behavior |
| --- | --- | --- |
| Keep | POST `/api/v1/jobs/{id}/keep`; target operation must be STAGED | Durable KEEP_MEDIA request, carried out by the Keep worker (KEEP_SONARR.md) |
| Cancel | POST `/api/v1/jobs/{id}/cancel`; target must be cancelable unfinished work | Fence target immediately. Enqueue cleanup if handlers must drain or resource receipts exist |
| Cleanup | POST `/api/v1/jobs/{id}/cleanup`; target already canceled/cancel-requested | Reconcile one bounded pass; defer for drain/external confirmation; stop for review on unsafe evidence |

Cancellation with no running members or recorded resources needs only a local
transaction plus a nonblocking exclusive target-lock check: cancel idle members
and mark cleanup complete, return 200. An expired/recovered database lease alone
is never accepted as proof that a stale handler stopped. This
never claims that unrecorded/unowned files were swept. Otherwise the cancellation
fence, idle-member transitions, cleanup job, action association and audit are
committed together before returning 202. Queue failure rolls the whole request
back. No post-response in-memory task or API process lifetime owns the work.

Completed reservation jobs remain cancelable until staging is committed. The
shared eligibility check includes episode reservations in the explicit operation
scope; another operation's reservation grants no capability. Reservations alone
do not require torrent/file cleanup, and cancellation does not release them.
Successful staging still closes acquisition Cancel.

User-facing episode-selection messages use Intake and the viewing destination
label is Staging folder. Existing trial configuration keys, job/reason/event codes,
translation keys and persisted records retain their identities. No migration or
media relocation is involved.

A receipt contains job_id, status_url, history_url, state, readable status,
notification (`Action submitted.`), reused and disable_action. Location points at
GET `/api/v1/jobs/{job_id}`. Poll that URL and its history until the job completes,
waits or needs attention. A queued acknowledgement is never a completion receipt.
After an uncertain HTTP outcome, repeat the explicit action; the database returns
the same job. Do not optimistically tell the user that Keep/cleanup succeeded.

The future dashboard should disable the clicked button while submitting, show
the receipt's human-readable notification, and use each target job's `actions`
object on subsequent reads to render availability. Keep/cancel/cleanup entries
contain enabled, disabled_reason, plus an existing action job/receipt where applicable.
Use disabled_reason.message for the disabled button tooltip; its code and
message_key come from the shared diagnostic catalog. Enabled actions have a null
disabled_reason. An active action job disables competing Keep/Cancel/Cleanup
buttons for that operation. Queued jobs say “An action is already queued.” Running
jobs say “An action is already running.” Waiting jobs say “A job is waiting to
continue.” Held, blocked, review, completed and canceled jobs must not be labeled
as running. Receipts also expose disabled_reason for the submitted action;
refresh action availability from the target read rather than using a stale receipt.
Keep the notification “Action submitted.” These messages explain state without
claiming the requested work is complete. Keep remains unavailable for execution;
execution_available and availability_reason must still be respected by clients. A repeated
click through another job in the same operation resolves to the same action.
Refreshing/restarting the API does not lose this state. UI HTML and actual
notification widgets are not part of this API change.

The `(operation_id, action)` key deduplicates intent, including across API
processes. A control job has its own ACTIVE operation and immutable target link,
not a provenance relationship. A cleanup job must never be placed in its canceled
target scope: that would cancel itself and conflict with its own shared gate.
Worker holds the control scope's shared lock and the existing maintenance
admission; cleanup takes the target scope's exclusive lock. No cleanup is allowed
until participating handlers (including blocking threads) drain. Control jobs are
not acquisition targets and are not offered Cancel/Keep actions.

Cleanup guards its own current lease in every mutating transaction and before
each external control or file deletion. The exclusive target lock stays held
through the pass and drains before a recovered control attempt can proceed.
Existing one-use stop/remove journal permits remain authoritative; an expired
lease, crash, retry or absence never authorizes a second uncertain command. A
previously sent external request cannot be revoked by a SQLite fence. File safety
and local cooperating-process assumptions remain as documented in CANCELLATION.md.

Pending cleanup defers 30 seconds without consuming failure retries or occupying
a worker slot. Unsafe ownership/files or client configuration enters review.
After fixing the cause, an explicit POST cleanup resumes the same stopped control
job, with a MANAGEMENT_ACTION_RESUMED history record; additional pending/running
clicks reuse it without changing its lease or schedule. This does not reset
failure history, discard uncertain journal phases, release reservations, or grant
new destructive permits. Keep and cleanup receipts remain immutable history;
there is no generic job deletion or arbitrary enqueue/retry route.

## Keep

A Keep request for a correctly staged target records one KEEP_MEDIA job, which
imports the episode into the library through Sonarr (see KEEP_SONARR.md). While
Sonarr's saved status says it cannot work, the target's Keep capability reports
execution_available=false with the reason, and the request is refused. Jobs that
stopped before Keep existed (KEEP_NOT_READY) were migrated to KEEP_IMPORT_REVIEW
by migration 064; they resume only through an explicit Keep request.

`jobs.management.registrations(secrets)` supplies the trusted cleanup and Keep
registrations. The composed identity worker installs them even without a metadata
adapter. The generic Worker still requires explicit trusted registrations; the
API does not assert that a worker is online. Existing provider readiness rules of
the identity-worker composition remain unchanged.

## Persistence and validation

SQLite v18 adds management_actions: target operation, allowlisted action,
representative target job, unique control job and creation time. One action per
target scope; separate control scope; existing incoming-event correlation is
retained. Existing jobs, histories, scopes and cleanup journals are preserved.
018 is explicit v17→v18; fresh schema and migration manifest agree. Published
001–017 are unchanged. No implicit database upgrade occurs during API startup.

Offline cases cover atomic rollback, duplicate/concurrent admission, API restart,
202/status/history/button metadata, immediate idle cancellation, authorization,
staged eligibility, explicit blocked Keep, drain locks, stale leases, recovery,
review resubmission, schema preservation/constraints/version guards and rollback.
Tests and CI remain unexecuted under project policy. No live API, client request,
media movement, cleanup, database migration or deployment was performed.

## Decision download cleanup follow-up

New completed Reject decisions and post-Keep staging cleanups enqueue a separate
owned-download cleanup job. Receipts expose `download_cleanup`; a stopped job
can resume through Keep/Reject with the dashboard label Retry download cleanup.
Decision/import completion and episode protection remain intact. Current contract:
[Download cleanup](DECISION_DOWNLOAD_CLEANUP.md). This supersedes earlier remaining-download-cleanup notes above. Tests remain unrun.

## Current Shoko and service setup contract

The latest draft adds optional Shoko AniDB metadata after Keep (configuration v10,
backward-readable secrets v6; SQLite stays v23). It also provides explicit local
setup/maintenance commands and composed worker/systemd examples. See
[Shoko metadata](SHOKO_METADATA.md) and [Service setup](SERVICE_SETUP.md).
These supersede earlier remaining-Shoko/supervision/version notes above.
Tests, migrations and services have not been executed.
