# Selected-video download

DOWNLOAD_TORRENT / download_torrent_job.py follows a confirmed stopped submission.
The Worker advances the pipeline by atomically completing SUBMIT_TORRENT and
creating this independent job with preparation_job_id. The job library implements
selection, one possible normal start and read-only progress checks. No business
pipeline logic is added to the Worker. No subprocess or long-running polling loop
is launched: bounded blocking requests use the Worker's existing drained executor,
while waiting returns a deferred job so other work can proceed.

Trusted metadata composition now follows:
IDENTIFY_ANIME → VERIFY_RELEASE → SELECT_USER_EVALUATION → RESERVE_EPISODE →
PREPARE_ACQUISITION → SUBMIT_TORRENT → DOWNLOAD_TORRENT.
Without the explicitly supplied trusted metadata reader it still ends at
reservation. A real metadata retrieval adapter is still future work.

## Evidence and file selection

Each attempt rechecks the same-event completed preparation and its reservation,
release, incoming-body digest, verified identities and frozen byte budget. The
submission's saved endpoint, isolated destination, category and timeout stay
pinned; private credentials are read fresh from the central secrets library.
Current hash, marker without a competing application marker, destination,
category, manual management and ordered index/path/size inventory must match.

The client reads torrent info/files before and after detailed file/properties
inspection; a changed snapshot defers. Responses, progress, priorities and piece
ranges are validated and bounded. These separate reads are not an atomic client
snapshot. Ownership and stopped state are checked again immediately before each
control request. External actors can still race these observations.

The selected video gets normal priority 1; every other file gets skip priority 0.
Selection uses metadata file indices, never a provider-supplied command or path.
Single-file sources need no priority write when already correctly selected.
A stopped torrent with the exact desired priorities must be observed before start.

**This first implementation requires zero unwanted boundary bytes.** A skipped
file may share a BitTorrent piece with the selected video. From the complete v1
inventory and the client's piece size/ranges, calculate the byte interval covered
by all selected-video pieces. If it contains any other file bytes, stop for review
with TORRENT_PIECE_OVERLAP before any selection/start request. Single-file sources
and piece-aligned selections can proceed. There is no overlap allowance or implicit
expansion of the frozen selected-video budget. Padding/v2/hybrid sources remain
outside the preparation contract. This strict policy can reject otherwise useful
multi-file releases; relaxing it requires an explicit policy/budget design.

## Durable control and recovery

SQLite v16 adds torrent_transfers, one row per submitted plan. Selection and
start have separate durable authorization steps. Each short journal transaction
checks the current lease/attempt, immutable source and saved submission; no network
operation holds a database transaction or takes another maintenance admission.
The recorded fingerprint binds the saved submission, fixed priorities, piece size
and zero-overlap policy. Transport rechecks that fingerprint against its latest
snapshot before consuming a control permit.

| Phase | Meaning and permitted behavior |
| --- | --- |
| SELECT_UNCERTAIN | Selection intent committed. Only its original process-local permit can send the fixed skip and wanted commands once each. Later attempts only inspect. |
| SELECTED | A stopped, exactly selected torrent was observed and committed. One new start authorization may be issued. |
| START_UNCERTAIN | Start intent committed before one possible request. Later attempts inspect; stopped state or absence never grants another start. |
| ACTIVE | Client reports an active/queued transfer with matching ownership and selection. Record progress and defer. |
| COMPLETE | Client reports selected-file progress 1 with matching provenance and priorities. Revalidate the journal in fenced queue completion. |
| REVIEW | Observed ownership, selection or geometry conflicts require human review. No automatic reset or control replay. |

Commands use normal start on qBittorrent 5.x or resume on 4.5/4.6, never force-start
or all-hash operations. Already-complete, exactly selected stopped content needs
no start request. No pause, delete, re-add, global setting, category creation or
media filesystem operation is implemented.

Crash after a selection commit can lose some or all of its action permits. Exact
already-applied priorities can still be confirmed; partial selection is left
uncertain. Crash after start commit loses the start permit. A timeout may mean
the server accepted a command: subsequent attempts inspect without replaying it.
Manual review/recovery tooling remains future work and must fence old processes
before changing durable authority. Elapsed time, retries, missing torrents, changed
configuration and lease expiry never rearm these commands.

SQLite fencing cannot revoke an external request after its final guard. An old
process can issue its single delayed control request; competing jobs receive no
replacement permit. There is no exactly-once external execution, atomic client
compare-and-start or guarantee of exclusive control over another application.
These limits apply to priority changes as well as start. A journal/lease rejection
prevents further authorized database completion, not already-sent network work.

Once a transfer journal exists, OWNED_STOPPED is a historical submission receipt.
Duplicate/recovered submission jobs preserve that receipt and may route another
download job; the transfer journal arbitrates control and fresh observations.
Submission reconciliation no longer interprets an active handed-off transfer as
an invalid stopped submission. A conflict in a transfer remains durable REVIEW.

## Progress, diagnostics and next boundary

Waiting uses RETRY_WAIT with WAITING_FOR_DOWNLOAD; active progress uses DOWNLOADING.
Both release the worker slot between checks. Selected-file progress is bounded
0–1 in SQLite; phase changes have durable job history independent of log levels.
Static readable event/reason messages live in the shared translation-ready catalog.
No response body, credential, cookie, tracker URL or raw exception is persisted.

COMPLETE means client-reported download completion only. It does not prove the
local file exists, is regular/safe, has expected bytes, contains the expected
episode/audio, or is ready to publish. No successor is installed yet. Local media
verification and safe staging into the configured preview folder are next;
Keep remains a separate explicit workflow.

Official API references checked during implementation:
- [qBittorrent 5 API](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-5.0%29): inclusive piece_range, piece_size, per-file progress/priorities, filePrio and start.
- [qBittorrent 4 API](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-4.1%29): normal resume and filePrio.

Offline cases cover geometry/overlap, exact control targets, malformed/stale
snapshots, duplicate jobs, timeout and lost permits, stale leases, historical
handoff, pinned settings, selected-only completion and v15→v16 preservation,
fresh equivalence and rollback. **Tests are written but unexecuted.** Source-only
review does not establish runtime compatibility. No live client/provider calls,
application or SQL execution, CI, downloads, media operations or deployment were
performed for this change.

## Cancellation follow-up

The v17 cancellation service now owns separately authorized stop/removal and
manifest-only file cleanup. It drains the explicit operation, preserves uncertain
external ownership and exposes readable cleanup progress. Even if this download
job is DONE, an unstaged acquisition remains cancelable. See ../CANCELLATION.md.

## Verification handoff

The default composition supplies VERIFY_MEDIA as the optional completion
successor. The worker commits download completion and this successor together;
the payload carries preparation_job_id and download_job_id. See
[VERIFY_MEDIA.md](VERIFY_MEDIA.md). Custom registrations can omit the successor.
No successful client progress report alone marks media verified or staged.
