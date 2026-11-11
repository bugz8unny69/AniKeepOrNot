# Recover failed episode reservations (NextGen)

This change introduces episode-scoped recovery without deleting historical
reservation rows or bypassing the existing qBittorrent owned-cleanup checks.

## Admin CLI

```text
adm recover resv --ep-id 12018298
adm recover resv --ep-id 12018298 --release
adm recover resv --ep_id 12018298 --release --json
```

`resv` abbreviates *reservations*. `--ep-id` accepts an exact persisted
AniDB or TVDB episode **ID**, not the episode number or a release name.
An ambiguous ID fails closed. The default prints the exact episode, historical
reservation states, manual candidate count and owning operation IDs **read-only**.

`--release` is a flag, not a job argument. It queues a
`RESET_EPISODE_SEARCH` worker job. Nothing is released by the CLI before
the worker proves cancellation and owned-resource cleanup; the CLI does
not edit SQLite reservations directly. It subsequently schedules one fresh
`DISCOVER_EPISODE_RELEASES` search for that episode.

## Intake controls

- **Retry acquisition** for a stopped but still active operation uses existing
  guarded job Retry. A size-failed, cleaned-up candidate offers a separate
  **Retry acquisition** button: it replays the same source, including a manually
  submitted `.torrent` if present, into a new `VERIFY_RELEASE` job using
  current settings. It does not blindly restart a canceled qBittorrent torrent.
- **Search / Search again** is available for unstaged anchors and
  `CANDIDATES` episodes without an active owner. These requests expose a
  **Delete cache** option.
- **Delete cache** schedules the same guarded reset as the CLI. Matching
  single-episode reservations and manual candidate operations are canceled
  and fully cleaned first. Batch operations owning other episodes are not
  partially canceled. Reset stops for review if any owner is staged, cleanup
  is unresolved, or the episode's identity is ambiguous.
- After cleanup, previously selected Prowlarr candidates, selections and
  search results are invalidated, and fresh search may reconsider an older
  size-rejected hash. Released reservations and job history remain as evidence.
  Active manual submissions are moved to `manual_release_archive` without
  uploaded metainfo; the old event and job stay accessible from Related jobs.
  This frees the per-episode/hash uniqueness slot for a later manual resubmission.

A `RESET_EPISODE_SEARCH` in flight is displayed as **Recovery pending** and
can be opened under **Related jobs**. Active reservations are distinguished
from previous reservations in the Intake display. The cleanup worker's
regular Retry remains responsible for resuming a stopped cleanup; afterward
the reset job can itself be retried.

A size failure (`ACQUISITION_SIZE_LIMIT` or
`ACQUISITION_PLAN_SIZE_LIMIT`) is a *policy failure*, not a permanent
blacklist. On an explicitly retried or replayed acquisition, size admission
uses current configuration, while ordinary automatic work continues using
the original plan's frozen budget. Increasing limits durably raises the
plan's captured budget, never retroactively lowers it.

## Migration and deployment

Schema v62 adds only `manual_release_archive`. Upgrade the SQLite database
using the existing explicit NextGen sequence after installing these changes
and stopping API/worker:

```bash
python -m src.anime database-preview --config-dir /etc/anime-discovery
python -m src.anime database-upgrade --config-dir /etc/anime-discovery \
  --plan-token <token-from-preview> --services-stopped
```

Use the installed program's equivalent service-user command if different.
Do not run API/worker with an older schema. The upgrade runner performs an
online database backup. Restart API and worker only after successful upgrade.

## Safety boundaries

The reset never issues raw `DELETE FROM episode_reservations` or
`DELETE FROM batch_episode_reservations`. Existing cancellation marks the
rows RELEASED after proof of cleanup and wakes waiters safely.
The episode cannot be staged or kept at reset time. A multi-episode batch
operation never gets canceled for just one episode. No second ACTIVE
reservation is granted to a manual or replayed release.

**Validation:** Regression tests were added. No CI was requested or triggered
for this branch; tests have not been run on the user's deployment.

## Cleanup stuck in CANCELLATION_PENDING

When an operation is already canceled and its jobs are DONE/CANCELED, a
`CLEANUP_CANCELED_OPERATION` job may nevertheless be waiting for the
exclusive operation lock. Previously the cleanup service suppressed this
`CleanupBusy` condition, reused an old `CANCELLATION_PENDING` reason
(sometimes from an earlier download-client error) and retried every 30
seconds without a limit.

The control job records `CANCELLATION_DRAINING` for a busy operation
lock. **Elapsed time alone never changes a transient lock or temporary
transport wait into `NEEDS_REVIEW`.** Cleanup stays queued with a gradually
longer retry interval (30 seconds initially, at most five minutes) until the
existing owned-resource cleanup succeeds. A genuine ownership contradiction
or unsafe filesystem result still goes to `CANCELLATION_REVIEW`.

Earlier releases incorrectly parked temporary waits in Needs Review after
15 minutes. On startup and worker reconciliation, the new worker identifies
*only those historical temporary-review outcomes* and automatically requeues
their cleanup action once it can obtain the operation's exclusive lock. This
does not change any reservation or torrent record: those are released by the
normal cleanup handler **after** owned-resource removal is confirmed.

To recover an old operation whose actual workers are already stopped:

1. Review `sudo adm report job --id <cleanup-job-id>`.
2. Restart `anime-discovery-worker.service` to release locks held by
   that process; verify no other AniKeepOrNot instance still uses the
   same database.
3. Old Needs Review states caused solely by the removed timeout are
   requeued automatically when the operation lock can be acquired. If it
   remains in RETRY_WAIT, the next scheduled attempt runs automatically.
   Genuine ownership-review states still require their recorded conflict
   to be resolved.
4. Confirm `CANCELED / COMPLETE` and that **all** single/batch episode
   reservations are `RELEASED` before starting new acquisition work.

A batch pack may reserve multiple episodes. Do **not** force-set its
reservations to RELEASED, delete its `ad-<marker>` folder, or modify the
job-operation status directly to work around an exclusive lock. These
operations can strand owned torrents or allow concurrent acquisitions.
