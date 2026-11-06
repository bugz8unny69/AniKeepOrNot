# NextGen job handlers

Explicit trusted registration only; every handler module ends in `_job.py`.
The worker owns lease validation, lifecycle, atomic facts/history/successor
commits and scheduling. Feature libraries own provider and domain decisions.

## Identify anime

`identify_anime_job.registration(resolve, release_kind=None)` installs
IDENTIFY_ANIME with the static readable label "Identify anime series". Queue
payload is `{}` and `jobs.event_id` supplies the durable delivery reference.
The handler reads at most 64 KiB of the stored incoming body through a short
read-only version-checked connection. Missing/oversized/ineligible events go to
review; another incoming-event lease is never bypassed. The body is untrusted,
hidden from request representations and never copied into diagnostic history.

The installed async identity library implements:

`await resolve(request, operation_configuration, cancellation_event)`

It returns a provider-verified AnimeSeries v2 dictionary. It must independently
verify AniDB anime identity, the TVDB relationship and the nonblank anime title;
webhook IDs, filenames and normalized title overlap are hints, not proof. Shared
provider throttling, response bounds, authentication and cancellation belong in
that library. Do not use global per-anime serialization or mutate jobs there.
Missing evidence raises IdentityNotReady; conflicting evidence raises
IdentityConflict (both from storage.sqlite). ProviderUnavailable requests a
failure retry; ProviderDeferred(delay_ms) requests a normal wait without spending
the failure retry budget. Unexpected exceptions use the existing safe worker
failure path. Required malformed facts go to review with IDENTITY_INVALID.

The handler freezes and bounds the validated result to 64 KiB. This initial
job rejects nonempty attributes instead of persisting arbitrary enrichment,
provider response blobs or credentials. Zero identified episodes is valid.
This proves series identity only; it authorizes no acquisition or file operation.

`persist_identity` reuses the existing SQLite identity rules inside the worker's
transaction; the public `save_identity` remains the standalone transaction-owning
wrapper. Exact prior delivery/anime snapshots are reused. Conflicting snapshots,
canonical TVDB IDs or episode owners are never overwritten. Another delivery
retains its independent result/provenance. Metadata corrections preserve earlier
snapshots under existing storage rules.

On completion, identity facts, the incoming event's SERIES_IDENTIFIED audit,
handler ending, job DONE and an optional permitted release job commit together.
The audit receipt is logged only after commit. Expected domain rejection rolls
back every tentative write before the worker records NEEDS_REVIEW under its
still-valid lease. Storage errors still stop/drain the worker; uncertain commits
are never blindly replayed. A stale worker cannot persist facts or successors.

The standalone registration has no successor by default. The concrete worker
composition now installs VERIFY_RELEASE and routes identity completion to it.
Only application composition can supply a release kind, and Registry requires
that kind to be installed. Its payload contains `anidb_id`; the successor retains
the same event_id and created_by_job_id provenance. That pair selects the durable
identity snapshot; the release job must verify exact episode coverage before
acquisition. It must not treat the series result as release approval.

## Current boundary and next work

The concrete AniDB-first resolver is now available in providers/identity.py;
see [Provider libraries](../providers/README.md) for composition, caching,
throttling and conservative verification gates. Application/service bootstrap,
producer intake adapters and installed service supervision are still required
before deployment. Provider settings/SecretStore and validated worker composition
are now documented in ../SECRETS.md. No service is deployed automatically.
The default normalizer accepts the explicit release_name JSON input contract;
it does not promise compatibility with every webhook source.

Declared single-episode release verification is now in verify_release_job.py;
see VERIFY_RELEASE.md. Intake service, exact file-manifest verification,
trial/acquisition and Keep handlers remain future work. Later PRs follow the architecture documents and settle their open
policy questions before acquisition, publication or deletion is implemented.

Reviewed legacy references at adf537ac58dc13826071039cc3c6f11ed4229479:
`anime_identity_verify.py`, `anime_identity_persistence_v8.py`,
`anime_identity_rejection_gate.py` and `anime_identity_worker_v16.py`.
Reuse verified identity separation and conflict safeguards; do not import the
legacy controller, chained worker loops, review flags or runtime SQL. Existing
NextGen schema/storage code is reused directly. SQLite is now v10 for shared provider cache/pacing; configuration
v4, AnimeSeries v2. No DDL or numbered migrations change.

Offline tests cover worker integration, atomic handoff/rollback, post-commit
receipts, unsafe payloads, invalid results, provider wait/retry, existing/conflicting
identities, episode ownership, lease fencing, incoming leases and caller rollback.
All tests are authored but **unexecuted** under project policy. No CI, application
imports/execution, SQL execution, live provider calls, deployment or media changes.


Trial evaluation now follows release verification in the composed worker.
EVALUATE_TRIAL records immutable plan/evaluation facts and readable decisions;
see ../USER_EVALUATION_POLICY.md. It never schedules acquisition. Configuration is v5,
SQLite v11 and AnimeSeries v2. Trial targets remain intents until later exact
resource/provider inventory and publishing prerequisites are implemented.

## Exact episode gate

The default composition now follows selection with RESERVE_EPISODE /
reserve_episode_job.py. See RESERVE_EPISODE.md for scope, hold/review behavior and
the durable reservation lifetime. No external acquisition successor is installed.

## Source inventory preparation

PREPARE_ACQUISITION / prepare_acquisition_job.py records hash-bound single-video
metadata and a stable submission marker after a reservation. It is composed only
when the application supplies a trusted async metadata reader. No reader/client
transport or external submission is installed by default. See PREPARE_ACQUISITION.md.

## Stopped submission and reconciliation

SUBMIT_TORRENT / submit_torrent_job.py follows preparation in trusted metadata
composition. It has one durable send authorization per plan, adds stopped, and
reconciles pinned client provenance/inventory. It never starts or deletes data.
See SUBMIT_TORRENT.md for supported versions, states and failure boundaries.

## Selected-video download

DOWNLOAD_TORRENT / download_torrent_job.py follows stopped submission in trusted
metadata composition. It durably selects the video, starts once and defers between
progress checks. Shared boundary pieces require review under the initial zero-overlap
policy. Client completion ends this stage; media verification/staging remains next.
See [DOWNLOAD_TORRENT.md](DOWNLOAD_TORRENT.md). Current SQLite is v16, configuration
v6, secrets v2 and AnimeSeries v2. Earlier sections describe incremental milestones.
