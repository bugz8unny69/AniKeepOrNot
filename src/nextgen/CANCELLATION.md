# Cancellation and owned-resource cleanup

Cancel is a capability of unfinished work, not a special response to one reason
code. The trusted management library returns capabilities through API v1 for
future dashboard/ADM clients. Dashboard HTML and its buttons remain future work.
Never invoke this library from
an unauthenticated route or accept a caller-provided torrent hash, path or manifest.

## State and capabilities

| Situation | Capability |
| --- | --- |
| PENDING, RETRY_WAIT, ON_HOLD, BLOCKED, NEEDS_REVIEW | can_cancel while the operation is ACTIVE |
| RUNNING | can_cancel; request a cooperative stop and drain before cleanup |
| An earlier job is DONE but another member is unfinished | can_cancel on the overall operation |
| All jobs DONE, with an episode reservation but no acquisition plan yet | can_cancel; staging is unfinished |
| All jobs DONE, with no episode reservation, acquisition resources or staging artifacts | No cancellation |
| Download/preparation DONE, resources remain and staging is unfinished | can_cancel; job completion does not mean completed staging |
| Verified staging committed as STAGED | No cancellation or cancellation cleanup |
| Cancellation already requested/canceled | No second Cancel; show can_cleanup and durable pending/review/completed status |

Capability reads and cancellation admission share the same operation predicate.
A reservation is unfinished acquisition evidence, not a torrent/file receipt.
If it is the only outstanding evidence and the operation lock confirms drainage,
the management API finishes cancellation locally without a cleanup control job
or client/filesystem deletion. If the lock is held, a control job waits for drain.
Reservation history and exclusivity remain intact; automatic slot release is a
separate policy. STAGED still disables Cancel even when reservations remain.

Independent jobs and created_by_job_id remain unchanged. SQLite v17 adds explicit
job_operations and job_operation_members as a cancellation boundary. This is not
an execution dependency graph, whole-anime serialization or replacement for jobs.
Each newly enqueued root gets a new scope. Worker-created successors explicitly
inherit its operation ID in the same completion transaction. Other independently
enqueued work stays independent even if it has the same event or provenance ID.
Only trusted library code can explicitly assign an existing ACTIVE scope.

Migration 017 gives each historical job its own scope. It deliberately does not
infer a workflow from created_by_job_id, event ID or anime identity. Existing v16
pipelines need trusted reconciliation before their separately scoped consumers
can be treated as one cancellation unit. Cleanup preserves resources referenced
by another scope, including a separately canceled scope; no unsafe automatic
adoption/release is provided. Fresh pipelines receive the explicit handoff scope.

CancellationStore.status(job_id) reports operation_id, state, cleanup_state,
reason_code, can_cancel and can_cleanup. API authorization must precede all calls.
CancellationService(configuration_snapshot,secrets).cancel(job_id) durably requests
cancellation then makes one bounded cleanup pass. request() can return promptly;
the management process can call cleanup() again for pending work. Each pass handles
at most one unfinished torrent by default (trusted max_targets bound 1–32). The
API must run these blocking library calls through a drained executor so unrelated
requests remain responsive. There is no
background scheduler installed here. Each pass reads fresh central credentials
for the saved client target. Construct a service with the current validated
operation configuration; existing resource paths/options remain pinned.

## Stop future work, then drain

The request transaction marks CANCEL_REQUESTED, records readable audit events and
cancels idle members without resetting their attempts or erasing prior history.
Running leases are retained until the attempt drains. New claims in the scope are
excluded. Renewal, completion and every existing ownership guard detect the
request. Completion cannot persist facts or create successors after the request.
Known preparation/evaluation/verification payload references also cannot enqueue
new consumers of a canceled scope through the normal storage API.

Every Worker attempt holds a shared per-operation filesystem lock across handler
execution, drained blocking work and completion. Cleanup needs the exclusive lock.
The ordinary maintenance admission is retained throughout both operations, so
migrations still drain them. This is a local-process protocol on the same canonical
local SQLite path, not a distributed lock. Separate hosts, old worker binaries or
code bypassing the storage/lock contract must be stopped before using cleanup.
An expired SQLite lease does not prove a process has stopped. A process crash drops
its lock; an old still-running process retains it and prevents destructive cleanup.

Cancellation is observed at existing ownership guards or the worker heartbeat.
It does not kill a Linux process or undo an external request. The async worker
signals cancellation and drains the handler/executor before releasing protection.
Unrelated operations continue normally. Cleanup is a management-process library
operation, never a Worker handler trying to take an exclusive lock it already
holds shared. Existing application maintenance locks are not nested.

## Torrent and download cleanup

Load only persisted plans belonging to this operation. Revalidate completed
preparation, reservation, release, incoming-body digest, identity, selected bytes
and frozen budgets. Check other explicit consumers. Unknown/external submission
ownership and uncertain stopped-add outcomes require review; absence is not a
license to delete arbitrary payloads.

For a proven-owned submission, recheck exact hash, marker without competing
application markers, isolated save directory, category, manual management and
ordered file inventory. The v17 cancellation_cleanup journal separately records:

| Phase | Behavior |
| --- | --- |
| PREPARED | Durable cleanup target; no external action implied |
| STOP_UNCERTAIN | One possible normal stop/pause authorized; later passes inspect without replay |
| REMOVE_UNCERTAIN | Exact stopped ownership and local file fingerprints recorded before one possible torrent deletion; later passes inspect without replay |
| FILES | Torrent absence observed; delete only matching recorded local files |
| COMPLETE | Recorded file cleanup completed; no further control commands |

qBittorrent 5 uses stop; 4.5/4.6 uses pause. Removal targets one exact hash with
**deleteFiles=false**. File deletion is separate, so a broad remote delete cannot
bypass local safeguards. A lost response, crash or timeout never rearms a stop or
remove request. Still-present uncertain targets remain pending for reconciliation;
operator recovery/reset tooling remains future work. Durable phases and audit
records precede external mutation, and SQLite transactions never span network I/O.

The pinned download directory must be an accessible local/shared payload root
matching the client's path and isolated ad-<marker> directory. Every directory
component is opened without following symlinks. Compare root device/inode and
exact relative file device/inode/size/mtime; reject nonregular files, hardlinks,
symlinks, oversized files, changed files, unknown extras or overlapping current
preview/permanent roots. .!qB/parts files outside the approved manifest are not
silently adopted. Missing roots before authorization require review. A server on
another host without the same shared filesystem is not supported for file cleanup.

Only exact manifest files are unlinked through directory descriptors, with
rechecks and directory synchronization. Missing files on a resumed pass are
observed as absent; replacement/unknown files are preserved for review. Empty
directories remain. There is no recursive folder deletion or deletion of a preview
or permanent library. Filesystems/client state are not one atomic transaction;
other applications can race them. Dedicated roots must have controlled writers.
No protection against a malicious administrator replacing files between a final
stat and unlink, or atomic qB compare-and-delete, is claimed.

## Failed staging versus completed staging

Staging is still future work. Its cancellation hooks are ready in
staging_artifacts.py and storage/cancellation.py:

1. The trusted staging library creates a unique .ad-<random>.partial file
   exclusively in its validated destination root and records record_temporary()
   and commits that receipt before copying. Receipt binds the directory and file
   inodes to the scope/job.
2. Before publishing, publication() records PUBLISH_UNCERTAIN. Only after actual
   verified durable publication may publication(confirmed=True) record PUBLISHED.
3. In the fenced completion transaction, mark_staged() closes the operation only
   for STAGE_USER_EVALUATION_MEDIA with current lease, all recorded artifacts PUBLISHED and
   every other member job DONE.
   It must represent completion of the entire operation's intended staging.

These trusted hooks do not implement publication or manufacture media proof.
Partial-copy failure leaves TEMP, which cancellation may remove after drain using
its exact receipt. A replaced/symlinked/shared temporary is preserved. An uncertain
publication requires review. Published media is preserved even if another step
failed. Successfully staged/completed work exposes no Cancel. A crash before a
new temporary's ownership receipt is durable leaves an unowned file for review;
there is no directory sweep to guess its ownership.

Cleanup retains immutable preparation/budget and episode-reservation evidence;
it does not silently release reservations or erase acquisition history. Reusing a
canceled episode slot requires a separate explicit release/replacement policy.
A cancellation status of COMPLETE concerns the recorded owned-resource cleanup,
not erasure of those historical facts or proof of arbitrary external file absence.

## Diagnostics and validation

Every added event/reason has a static human-readable English catalog entry;
future translations key on codes. No raw exception/provider payload, credential,
cookie or tracker URL is recorded. can_cleanup remains available for pending or
review outcomes so the future UI can show progress and recovery separately from
Cancel. Keep/reject/removal of published preview content is a separate workflow.

Legacy references reviewed: anime_job_management.py and anime_dashboard_queries.py
restrict old Cancel to NEEDS_REVIEW; anime_cancel_cleanup.py provides durable
uncertain cleanup and ownership/sharing checks; anime_cleanup_file_safety.py
protects roots, symlinks and hardlinks. NextGen reuses those invariants without
importing the legacy runtime or holding SQL write locks across network deletion.

Offline cases cover all idle states, idempotence, scope versus provenance,
running-work drain, blocked handoff, owned cleanup, timeout/lost permits, changed
files, unknown/symlink/hardlink paths, shared consumers, staging partial/uncertain/
completed outcomes and v16→v17 preservation/equivalence/rollback. Tests and CI
remain **unexecuted** under project policy. Only source review, AST syntax parsing
and DDL/text checks are permitted for this change; no live cleanup was performed.

Official transport references: [qBittorrent 5 API](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-5.0%29)
and [qBittorrent 4 API](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-4.1%29).

## API v1 wiring

The API now exposes authenticated GET cancellation status and POST cancel/cleanup
at /api/v1/jobs/{id}/..., using this service library. Management Bearer or browser
session plus Origin/CSRF is required; webhook credentials cannot reach cleanup.
No GET performs cleanup, and a canceled command may return 202 for pending/review
cleanup. Blocking service work is drained on the API executor. Dashboard HTML and
automatic episode-slot release remain separate. See [API v1](api/README.md).

## Durable HTTP action submission (v18)

The versioned API now calls ManagementActions instead of performing cleanup in
its request handler. Idle cancellation with no recorded resources returns 200;
otherwise cancellation and a distinct cleanup control job commit atomically
before 202. The Worker executes bounded passes under the existing exclusive
target drain gate. Read [MANAGEMENT_ACTIONS.md](MANAGEMENT_ACTIONS.md) for status
receipts, duplicate clicks, explicit review resume and lease fencing. Earlier
examples of direct CancellationService calls remain trusted library usage, not
the current HTTP execution model.


## Canceled reservation release — v23

After drained cancellation cleanup completes, reservations and associated
acquisition plans are marked RELEASED in the same completion transaction.
Their history remains; active partial unique indexes and budgets let a later
verified delivery acquire the episode/hash again. Kept/staged duplicate
protection remains ACTIVE. Older completed cancellations need an explicit
Cleanup action to retire retained slots; migration does not release them.
This supersedes earlier paragraphs describing slot release as future work.
See [Reservation release](RESERVATION_RELEASE.md). Migration/tests remain unrun.
