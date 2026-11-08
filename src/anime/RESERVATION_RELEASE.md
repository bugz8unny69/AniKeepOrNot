# Releasing canceled acquisition reservations

SQLite v23 adds ACTIVE/RELEASED reservation state, ending time/job provenance and
partial unique indexes for active AniDB/TVDB episode slots. Acquisition plans
retain their IDs, manifests and markers; PREPARED/RELEASED state and a partial
unique infohash index allow the same torrent to be prepared again after cleanup.
Released rows stop contributing to the plan's active size budget. The original
budget limits and frozen episode-selection plan are retained.

A slot is released only inside the transaction recording CANCELED/COMPLETE,
while the existing exclusive operation lock proves workers have drained. Known
owned torrent/file cleanup must already be complete; shared consumers, uncertain
client outcomes, running work and published staging prevent release. The new
release event has a catalog reason and preserves prior job/reservation history.
No old canceled job is restarted: a later verified delivery creates new jobs,
reservation, preparation, marker and submission intent. Released source records
cannot authorize another acquisition or submission.

Kept/staged operations remain STAGED and retain active reservation/duplicate
protection. Reject decisions remain independent. This change does not remove
permanent media, restore rejected anime or search for missing episodes.

The API's reservation-only cancellation path can release locally after acquiring
the same drain lock; if a separate consumer prevents release, it records the
cancellation and queues the existing cleanup control job for review. Existing
completed cancellations with retained active slots expose Cleanup again. An
explicit cleanup rechecks the old journal and can retire the retained slot,
including resuming an old DONE cleanup job. Migration does not automatically
release historical slots or contact the download client.

Migration 023 rebuilds the two tables under drained maintenance, preserving row
IDs, data and foreign-key targets. The migration runner validates structure and
foreign keys before commit and restores FK enforcement after failure. SQL 001–022
are unchanged. Existing v22 databases require the explicit upgrade; there is no
runtime automatic upgrade. Configuration stays v9 and secrets v5.

Offline cases for cancel/reacquire of the same episode/hash, active budgets,
history preservation, source fencing, staged/kept protection, pending cleanup,
older completed cancellations, migration preservation/equality and rollback are
authored but unrun. Python/source checks do not establish migration correctness.
No SQL, tests, CI, live services, media deletion or deployment was run.

Remaining work includes download/torrent cleanup after Keep/Reject, optional
Shoko integration, service lifecycle/setup and end-to-end lab validation. Existing
cancellation download cleanup is reused, rather than being reimplemented here.

## Sequential waiting-release handoff

After canceled acquisition cleanup proves its owned resources are gone, releasing
one ACTIVE episode slot wakes **only the oldest eligible** ON_HOLD /
EPISODE_RESERVED contender, ordered by creation time and job ID. Later matches
stay on hold. This avoids waking several candidates into the same contention
race; it does not change exact AniDB/TVDB reservation exclusivity.

The worker also reconciles unreserved episodes during startup and polling.
If a woken claimant fails or stops *before* obtaining the reservation, and no
other matching reservation or queued claimant exists, it wakes the next eligible
contender. This handoff is idempotent, transactional and restart-safe. A staged
winner still cancels all other held competitors via the existing completed-stage
reconciliation. The worker never chooses a different episode or releases a
reservation merely because its owner is in Needs review.

Intake exposes failed ownership and a guarded **Try next release** action.
That action is the existing operation Cancel + cleanup; the handoff occurs only
when cleanup actually releases the reservation. **Retry acquisition** resumes
the same stopped job and retains its slot. Explicit **cancel every contender and
search Prowlarr again** is *not* implemented here: it requires a durable group
recovery intent to suppress waiter wake-up and only queue a fresh search after
all affected operations finish cleanup.

## Staged-episode competitor reconciliation

When one single-episode operation reaches the durable `STAGED` milestone,
the worker cancels other `ON_HOLD / EPISODE_RESERVED` single-episode jobs
whose independently verified AniDB/TVDB episode identity matches exactly.
Unlike a later arrival, which can complete as `SKIPPED_DUPLICATE`, a held
contender is `CANCELED` with reason `EPISODE_ALREADY_STAGED`.

The worker requests cancellation for the **whole competing operation**, not
just one job, and atomically queues its normal
`CLEANUP_CANCELED_OPERATION` control job. This existing cleanup path
handles resources owned by the loser and records pending/review/complete
outcomes; it does not delete the winning operation's staged media or
release its reservation. No external cleanup is performed inside the
staging completion transaction.

On startup and during polling, the worker reconciles old held competitors
against confirmed `PUBLISHED` receipts in `STAGED` operations. This covers
missed notifications and restarts without a manual repair script. It is
idempotent: a canceled operation cannot generate duplicate cleanup actions.
Unpublished/partially staged work, invalid identity matches, or holds for
other reasons are never automatically canceled.

## Decision download cleanup follow-up

New completed Reject decisions and post-Keep staging cleanups enqueue a separate
owned-download cleanup job. Receipts expose `download_cleanup`; a stopped job
can resume through Keep/Reject with the dashboard label Retry download cleanup.
Decision/import completion and episode protection remain intact. Current contract:
[Download cleanup](DECISION_DOWNLOAD_CLEANUP.md). This supersedes earlier remaining-download-cleanup notes above. Tests remain unrun.
