# Exact episode reservation

RESERVE_EPISODE / reserve_episode_job.py is the next preparation step after
SELECT_USER_EVALUATION. It reserves a selected, provider-verified received episode before
future acquisition. It does not start a download or assert ownership of a torrent,
file, folder or content hash. A completed reservation job means only that the gate
has been recorded. Acquisition, reconciliation, staging and Keep remain separate.

The installed pipeline is IDENTIFY_ANIME → VERIFY_RELEASE → SELECT_USER_EVALUATION →
RESERVE_EPISODE. Each selection evaluation queues the gate with only its own job
ID; the decision and successor creation are atomic. The gate reads the committed
evaluation in its own attempt. An outside-window evaluation completes the gate
without taking a reservation and records EPISODE_RESERVATION_SKIPPED with the
readable TRIAL_OUTSIDE_WINDOW reason. This uniform handoff avoids a routing
prediction based on a window that a concurrent evaluation could anchor first.
Standalone selection registration retains an optional successor for offline or
custom compositions. No dynamic module loading is added.

## Evidence and exclusivity

The gate requires a completed SELECT_USER_EVALUATION job for the same incoming event,
completed VERIFY_RELEASE evidence with DECLARED_SINGLE_EPISODE scope and official
TVDB order, a consistent frozen selection plan/anchor, an unchanged identity
snapshot and incoming-body digest, canonical series identity and exact AniDB
episode ownership. It checks these again inside the worker's fenced completion
transaction. Payloads cannot provide their own episode identity or selection.

The unique slot is (TVDB series, official order, season, episode), with unique
AniDB episode and TVDB episode IDs as additional collision checks. Different
episodes of the same anime can proceed concurrently. A matching episode reserved
by another job yields ON_HOLD / EPISODE_RESERVED. Disagreeing AniDB ownership or
provider placement yields NEEDS_REVIEW / EPISODE_RESERVATION_CONFLICT. Sharing a
TVDB series is allowed; disagreeing over the exact episode is not silently merged.
Season zero works when the earlier verified mapping proves that placement.

Reservation, audit history and job completion commit atomically. A rollback or
expired worker lease cannot leave a reservation behind. Application log receipts
are emitted after commit; durable history is independent of verbosity. Codes,
messages, actions and translation keys use the shared catalogs.

## Lifetime and recovery boundary

The durable owner is the reservation job, not its transient worker lease. A DONE
reservation job still owns its slot. Restart, retry, elapsed time, canceled jobs
or worker lease expiration never transfer it automatically. The same owner and
facts are idempotent within the storage adapter. A competing job is held rather
than marked as an acquired/staged duplicate: no file has been proved to exist.

There is intentionally no release/delete/TTL/staged API yet. Future management
must reconcile external intents, receipts and artifacts before releasing or
transferring a reservation, then explicitly resume held work. This gate alone
cannot fence a stale external client or authorize an external side effect.

Only the verified received episode is reserved. Backfill/future numeric targets
need actual provider ownership, release and file evidence before they can obtain
slots. No provider IDs, search results or pack contents are invented.

SQLite v11→v12 adds episode_reservations through 012_episode_reservations.sql,
the fresh schema and runner in the same change. Historical SQL 001–011 remains
unchanged. Configuration stays v5 and AnimeSeries stays v2.

Legacy references reviewed: anime_acquisition_gate.py (approval/evidence gate),
anime_episode_acquisition_guard.py (atomic exact-episode slot),
anime_acquisition_enqueue.py (correlated enqueue), qb_trial_add_adapter.py
(uncertain add requires independent inventory/journal). Their invariants are
adapted; no legacy runtime import or hot-path DDL is reused.

Next item: design and implement trusted source/file inventory plus a durable
external acquisition intent and reconciliation contract before adding any
qBittorrent submission route. Backfill search also needs its own provider-backed
release discovery. Source selection, download ownership and staging are distinct
from this reservation.

Offline cases include competing releases, independent episodes, season zero in
default composition, outside-window decisions, identity conflicts, wrong-event
payloads, source changes, stale leases, completion rollback and migration
preservation/constraints/fresh equivalence/rollback. Tests remain unexecuted.

## Preparation follow-up

When a trusted metadata-reading library is supplied at composition, selected
reservations now enqueue PREPARE_ACQUISITION atomically. Outside-window skips do
not. Without that dependency, reservation still ends the installed pipeline.
See PREPARE_ACQUISITION.md for the PREPARED-only durable inventory boundary.

## Successful staging closes duplicate candidates

A matching active reservation keeps competitors ON_HOLD until its acquisition has a PUBLISHED staging receipt. Staging completion atomically marks exact verified held competitors DONE with SKIPPED_DUPLICATE and appends their history receipts. No acquisition successors are created. Canceled operations, inconsistent identities and other episodes remain unchanged.

Late or concurrently running reservation jobs recheck the published receipt in their fenced completion transaction. A duplicate produces a skip outcome, which finishes without publishing facts or routing acquisition successors. Copy attempts, partial files, completed identification and completed reservation jobs alone never establish successful staging. Keep remains a separate user decision.

The jobs API labels completed steps by their actual result, including Series identified, Episode verified and Staged. Unknown completed job kinds use Step completed. Skipped duplicate takes precedence over step labels.
