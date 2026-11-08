# Inspect source inventory and prepare acquisition

PREPARE_ACQUISITION / prepare_acquisition_job.py is the preparation library after
an exact-episode reservation. Its durable result is a PREPARED acquisition plan,
not a submitted torrent, proof of ownership, file-content verification or download
permission. There is no torrent add/start/stop/selection/delete transport here.

## Installed dependency and normalized input

An application may pass the async read_metainfo(source, configuration, cancelled)
library dependency to build_identity_worker/run_identity_worker. The callback
must read metadata only and return bounded complete .torrent bytes for the
source's exact content hash; it must not submit a magnet or transfer payloads.
The application owns endpoint authentication, tracker access, timeouts, cache,
pacing and cancellation. No URL, filesystem path, module or callback is selected
by the job payload. No plugin/hook loader or provider enable setting is added.
Startup does not call the reader. Without the dependency, the existing composed
pipeline ends at RESERVE_EPISODE; no unusable preparation job is silently queued.
This PR provides no default metadata transport and performs no live reads.

When the reader is installed, successful selected reservations atomically queue
PREPARE_ACQUISITION with only evaluation_job_id, preserving event and provenance.
Outside-window gates do not queue preparation. The reader runs outside SQLite
write transactions and receives a fresh operation configuration snapshot.

The initial normalized incoming JSON extends the existing release_name contract
with torrent_infohash: exactly 40 hexadecimal characters representing a v1
BitTorrent info hash. It is normalized to lowercase. Missing, malformed and
v2-only identities go to review before the reader is called. Producer-specific
Autobrr/Prowlarr adapters, magnet/URL parsing and metadata retrieval remain future
work; raw trackers/passkeys or webhooks' claimed file lists are not copied into
plans. The hash identifies the requested source, not trust in its contents.

## Inventory checks and scope

acquisition/metadata.py parses bounded BEP 3 bencoding, rejects duplicate/unsorted
keys and noncanonical integers/lengths, and computes the v1 hash from the exact
encoded info substring. It also records SHA-256 of those bytes. The source hash
must match. Files retain metadata order, indices, safe relative paths and exact
byte sizes; the piece-hash count must agree with total size and piece length.
Limits: 4 MiB metainfo, 16 nesting levels, 100000 decoded values, 10000 files,
16 file-path components, 2048 characters per stored path, 1 MiB serialized
inventory, signed-64-bit sizes/totals and a maximum 64 MiB piece length. These
are parser bounds, not a user download-size budget.

The initial scope is exactly one .mkv or .mp4 video, optionally accompanied by
.nfo/.srt/.ass/.ssa/.txt sidecars. Packs, multiple/no videos, unknown extra payloads,
v2/hybrid metadata, symlinks/attribute records, alternate UTF-8 path aliases,
absolute/traversing/ambiguous paths, control characters, duplicate/case-colliding
names and file/directory collisions go to review. No path is opened or joined to
a real media root. No sidecar is interpreted or executed.

The selected basename must independently declare the already verified regular
AniDB episode number or exact TVDB season/episode, and its parsed title alias
must match the canonical title. Missing/ambiguous aliases or numbering require
review, rather than guessed equivalence. This is declaration alignment only:
filenames and hashes do not establish actual anime, audio tracks or media bytes.
Verification of downloaded content remains a later job.

## Atomic plan and provenance marker

storage/acquisition.py rechecks the completed reservation owner, evaluation,
selection plan, release evidence, incoming body digest and canonical identity
before reading, and again inside fenced completion. Payloads cannot choose
replacement identity, reservation, file list or selected index.

SQLite v13 acquisition_plans binds a unique preparation job, reservation and
content hash to bounded inventory JSON, info SHA-256, captured configuration
revision and a random unique submission marker. Plan/marker, durable readable
audit and job completion commit together; rollback and stale leases cannot leave
a plan behind. A second preparation job or contradictory facts require review;
they cannot replace the first plan or marker. The marker is provenance, not a
credential, and is excluded from job payloads/logs. No raw metainfo, tracker URL,
passkey, cookie or provider credential is stored in this table.

PREPARED is the only supported persisted state in this change. Do not label it
submitted, downloading, owned or complete acquisition. The plan deliberately
contains no future guessed save path or content-hash collision ownership proof.
Configuration remains v5; AnimeSeries remains v2. 013_acquisition_plans.sql,
fresh schema, helpers and offline cases ship together; SQL 001–012 is immutable.

## Submission and reconciliation contract for the next change

Before any future submission, define validated client secrets/endpoint and
service-owned download root, exact per-episode/plan size policy for configurable
windows, payload selection/piece-overlap behavior and a lease-fenced durable
external operation intent. A PREPARED plan alone cannot authorize an add.

Persist the immutable request fingerprint, destination and marker, then record
an uncertain/submitting state *before* an external add. Only one fenced claim
may begin submission. Check exact-hash client inventory first; an existing swarm
is external until independent ownership proof exists. Category and successful
HTTP response alone are insufficient. Confirm the stable marker, hash, destination
and file inventory through authoritative client observations before claiming
control. No database transaction spans the HTTP call.

A crash, timeout, successful response with missing inventory, lost lease or
unknown add outcome requires read-only reconciliation by the persisted intent;
absence does not prove an add never happened and must not trigger blind replay.
Do not expire the marker or steal the reservation. External fencing/idempotency
must be designed for stale clients; SQLite fencing alone cannot prevent an old
process from sending a delayed request. Cleanup requires its own ownership proof.
Those states/transports/reconciliation routines are **not implemented** here.

Legacy references reviewed: anime_qb_manifest_intake.py, anime_single_size_intake.py,
anime_qb_submission.py, anime_external_actions.py, anime_action_reconcile.py,
qb_inventory_adapter.py and anime_claimed_pipeline.py. Reuse the invariants,
not hot-path DDL, legacy enable flags, fixed six-episode budgets, paths or mutable
legacy dispatch tables. The new preparation can inspect available metadata
without submitting first; a future metadata-only magnet path needs its own
explicit ownership/uncertainty design when metainfo is unavailable.

Protocol reference: [official BEP 3](https://www.bittorrent.org/beps/bep_0003.html).
It defines the exact encoded info hash, sorted dictionary keys and file fields.
Unsupported v2/hybrid formats require their own BEP 52 implementation.

Offline cases cover canonical/hash validation, bounded/path-safe inventories,
packs/aliases/links/piece counts, episode/title mismatch, missing source, racing
preparations, changed evidence, expired leases, atomic rollback, optional trusted
composition and v12→v13 preservation/fresh equivalence/constraints/rollback.
Tests and CI remain unexecuted under project policy.

## Download setting and size-admission follow-up

Configuration v6, private download credentials and SQLite v14 now implement the
local client settings plus frozen per-video/plan hard limits. PREPARED completion
records selected bytes and admits them atomically with its plan/marker. Legacy
v13 unknown sizes require review, not a zero-size assumption. This supersedes
the earlier size-setting prerequisite; client transport, payload/piece selection
and durable uncertain/submission/reconciliation states remain future work.
See ../DOWNLOAD_CONFIGURATION.md.

## Stopped-submission follow-up

Trusted metadata composition now atomically routes preparation to SUBMIT_TORRENT.
Its SQLite v15 journal pins the request and consumes one send right before a
possible stopped add. Timeout/crash retries reconcile rather than resubmit.
Transfer selection/start and a real metainfo retrieval adapter remain separate.
See SUBMIT_TORRENT.md; PREPARED still does not mean downloaded or externally owned.
The default composed identity worker now supplies a real hash-addressed metadata
reader and installs preparation through download. Explicit trusted readers remain
supported. Missing source settings stop at preparation with a readable review
reason. See [the reader contract](../acquisition/METAINFO_SOURCE.md); this
supersedes older default-reservation-only/reader-unimplemented notes here.
