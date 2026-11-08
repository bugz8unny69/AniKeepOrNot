# Job states and operational activity

SQLite v5 adds the missing legacy operational distinctions while keeping
scheduler lifecycle, activity and reason separate. This is schema/catalog
support; no worker, dashboard or acquisition handler is implemented here.

## Stored lifecycle: jobs.state

| State | Readable label | Meaning |
| --- | --- | --- |
| PENDING | Queued | Awaiting its first or explicitly requeued execution |
| RUNNING | Running | A worker owns a processing lease |
| RETRY_WAIT | Waiting for another attempt | Work may be claimed when its retry time arrives |
| ON_HOLD | On hold | Waiting for a specific condition to change, not an ordinary timed retry |
| NEEDS_REVIEW | Needs review | Requires human attention |
| DONE | Completed | This independent job completed |
| BLOCKED | Blocked | A policy or prerequisite prevents execution |
| CANCELED | Canceled | Work was explicitly canceled |

PENDING remains the default. Future claiming must consider only PENDING and
due RETRY_WAIT jobs (plus explicit expired-lease recovery). ON_HOLD, BLOCKED,
CANCELED, NEEDS_REVIEW and DONE must not be claimed merely because their
available_at_ms is in the past. This policy is not a scheduler already running.

ON_HOLD preserves the legacy condition wait: another release owns an episode
slot. It does not introduce parent/child jobs or mean the job is waiting for
manual approval. A future condition handler can explicitly requeue it once the
condition changes. Legacy used DEFERRED plus ON_HOLD; NextGen makes that
lifecycle distinction explicit. Whole-application maintenance remains a
separate processing gate, not a rewrite of every queued job to ON_HOLD.

Canceling a job does not imply its external download stopped or that any files
were deleted. Those effects require explicit, independently validated actions.
Blocked and canceled are distinct; no cancellation is inferred from a reason.

## Stored activity: jobs.activity_code

The nullable typed activity field describes operational progress/outcome.
job_history.activity_code captures its value at each audit insert. Both default
to NULL for older rows. No arbitrary external text is accepted as an activity.

| Activity | Allowed lifecycle states | Readable label |
| --- | --- | --- |
| NULL | Any | Use the lifecycle label |
| DOWNLOADING | RUNNING or RETRY_WAIT | Downloading |
| WAITING_FOR_DOWNLOAD | RETRY_WAIT | Waiting for download |
| WAITING_FOR_SEARCH | RETRY_WAIT | Waiting for search |
| SKIPPED_DUPLICATE | DONE | Skipped duplicate |

DOWNLOADING preserves legacy's indication of an active download stage. It is
not proof of nonzero transfer speed. SKIPPED_DUPLICATE means this job completed
without repeating already handled work; it is not intake deduplication.
Every incoming delivery must still be retained independently.

When moving into a held/review/blocked/canceled state, clear incompatible
activity in the same UPDATE; SQL rejects invalid state/activity pairs. Updating
activity, state and corresponding history belongs in the same transaction.
The worker must still fence stale workers and validate transitions.

## Legacy display mapping

| Legacy display status | NextGen representation |
| --- | --- |
| RECEIVED | incoming_events.state=PENDING; label Received |
| IDENTIFYING | incoming_events.state=RUNNING; label Identifying |
| QUEUED | jobs.state=PENDING |
| RUNNING | jobs.state=RUNNING without a more specific activity |
| DOWNLOADING | jobs.activity_code=DOWNLOADING |
| WAITING FOR DOWNLOAD | jobs.state=RETRY_WAIT, activity=WAITING_FOR_DOWNLOAD |
| WAITING FOR SEARCH | jobs.state=RETRY_WAIT, activity=WAITING_FOR_SEARCH |
| ON HOLD | jobs.state=ON_HOLD |
| SKIPPED DUPLICATE | jobs.state=DONE, activity=SKIPPED_DUPLICATE |
| NEEDS REVIEW | jobs.state=NEEDS_REVIEW or incoming_events.state=NEEDS_REVIEW |
| COMPLETED | jobs.state=DONE without a more specific outcome |
| BLOCKED | jobs.state=BLOCKED |
| CANCELED | jobs.state=CANCELED |

These are semantic mappings, not an import of a legacy database. Existing
NextGen rows keep their exact lifecycle state and receive NULL activity.
last_reason_code and history reason_code continue to explain why; rendering
does not parse those fields or raw exception text to guess a state.

## Shared messages and history

MessageCatalog.job_status(state, activity_code, locale=...) resolves both
the lifecycle and activity labels using the central catalog. Keys are
state.job.<CODE>.label and state.job_activity.<CODE>.label. Unknown or
incompatible historical activity falls back to the lifecycle label so stale
download metadata cannot mask CANCELED, BLOCKED, ON_HOLD or NEEDS_REVIEW.
Unknown lifecycle values receive generic Unknown status text.

ON_HOLD, BLOCKED and CANCELED lifecycle audit events each require a registered
reason and a matching attempt snapshot (which may be zero before first claim).
Activity is recorded on the event that carries it, for example DEFERRED with
TORRENT_DOWNLOAD_WAITING (ACTIVITY_CHANGED was removed in migration 064). The activity snapshot is captured from the job row, never supplied as
an unverified log string. A DONE audit with SKIPPED_DUPLICATE retains that
outcome in history. Every emitted code/activity has central English copy and
stable translation keys. No domain-specific reasons are invented ahead of
their actual handlers.

## Schema maintenance and validation

005_job_statuses.sql upgrades v4 to v5. It rebuilds jobs to widen its CHECK
constraint and add current activity, preserving all existing columns and
rows; it adds the activity snapshot column to job_history. All job indexes
and event/self-provenance/history foreign keys are retained and checked.

Run only during maintenance, without an existing transaction or other DB
users. Because referenced jobs is rebuilt, foreign_keys is disabled before
BEGIN. The caller must ROLLBACK on error and restore foreign_keys=ON in a
finally block. The script restores it on success. No production runner is
implemented or invoked. Historical migrations 001 through 004 stay immutable.
initialize() creates only a fresh v8 database or accepts v8; older versions
require explicit upgrades.

Offline tests cover state/activity constraints, messages, historical snapshots,
fresh/sequential schema agreement, data preservation and failed migration/FK
restoration. They are committed but not executed. No CI runs or live upgrades.

The subsequent v6 migration only adds api diagnostic attribution; job
states/activity and their history snapshots retain the v5 meanings above.


The job process is now named worker. SQLite v7 only adds that diagnostic emitter
while retaining historical coordinator attribution; no state/activity changes.

## Cancellation capability (v17)

Cancel is available for ACTIVE operations with PENDING, RETRY_WAIT, ON_HOLD,
BLOCKED, NEEDS_REVIEW or RUNNING work, and for completed download jobs whose
acquisition resources remain unstaged. Finished internal jobs remain DONE for
audit; the operation tracks the cancellation boundary. STAGED disables Cancel.
Running work drains before cleanup. Cleanup pending/review/complete is separate
from job lifecycle and its recovery action remains visible. The management library
returns can_cancel/can_cleanup; no dashboard or HTTP route is installed yet.
See [CANCELLATION.md](CANCELLATION.md).
