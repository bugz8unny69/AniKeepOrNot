# AniKeepOrNot NextGen — Batch/OVA implementation tracker

Updated: 2026-10-26. Target branch: NextGen.
Reference: legacy batch-selection design on develop.
Work tracked in draft PR #149. Separate review retry infrastructure: draft PR #150.

**This draft branch implements batch parsing, provider verification, policy
planning and durable schema primitives. No worker or endpoint is wired to
execute them yet. DO NOT DEPLOY until the remaining pipeline and migration
checks pass.**

## Implemented in PR #149 (not merged)

- [x] **NG-BATCH-00:** Document boundaries, dependency order and handoff.
- [x] **NG-BATCH-01:** Advisory single-episode, season-pack, season+OVA,
  special and ambiguous-title classification; title is never authority.
- [x] **NG-BATCH-02:** Hash-verified, bounded multi-video BEP3
  BatchInventory; existing Manifest still strictly single-video.
- [x] **NG-BATCH-03:** Pure filename parser and E01-first legacy-style
  selection with explicit episode mappings, fail-closed unknowns,
  duplicates, wrong season, ambiguous multi-episode files and extras.
- [x] **NG-BATCH-04 (provider primitive):** Validate every regular video
  against the AniDB episode catalog, Anime-Lists forward/reverse mapping and
  unique TVDB episode records, including offsets and season-two naming.
  Fails closed on missing/conflicting evidence and validates torrent identity
  before planning. Not yet wired into VERIFY_RELEASE.
- [x] **NG-BATCH-05 (storage primitive):** Add schema v27
  batch_verifications / batch_verified_files; migrate v26 -> v27;
  provide immutable per-file proof persistence under a fenced worker lease.
  No worker completion handler or downstream reservation consumes these yet.
- [x] **NG-BATCH-06 (pure policy primitive):** Intersect full verified
  batch episode evidence with current Window(backfill_count,
  following_count), including anchors beyond the initial episodes.
  No durable window budget/reservation or torrent selection authorization yet.

## Remaining integration and verification

- [ ] Wire NG-BATCH-04 into VERIFY_RELEASE and add typed batch completion
  alongside the existing single-episode pathway. Preserve single releases.
- [ ] Wire NG-BATCH-05 persistence into the worker's fenced completion
  transaction, then expose a separately typed source for trial evaluation.
- [ ] Finish NG-BATCH-06 with durable trial/window commitments, episode
  reservation arbitration, size budgets and explicit partial-pack handling.
- [ ] **NG-BATCH-07:** Owned torrent submission, stopped/stable manifest
  reconciliation and per-file selection: download probe first; unlock remaining
  only after independently verified durable JP+EN audio proof.
- [ ] **NG-BATCH-08:** Per-file download verification, stage, Keep/Reject,
  cancellation, ownership-safe cleanup and duplicate staging behavior.
- [ ] **NG-BATCH-09:** Explain mapping and file-exclusion reasons in bounded,
  durable VERIFY_RELEASE/batch evidence.
- [ ] **NG-BATCH-10:** Dashboard episode coverage and selected/skipped file
  diagnostics, distinct from anime identity matching.
- [ ] **NG-BATCH-11:** Explicit review retry endpoint/worker handoff,
  separately owned in PR #150 / another chat. Do not duplicate that code.
- [ ] **NG-BATCH-12:** Offline full integration tests with fake AniDB, TVDB,
  qBittorrent, worker crash/restart and SQLite transactional proof.
- [ ] **NG-BATCH-13:** Full repository CI and upgrade/migration acceptance
  after user explicitly permits running them; production deployment.

## Safety invariants

- An incoming release title and torrent filenames are *hints*, not proof.
- BatchInventory contains no selected_index and cannot satisfy the existing
  single-video Manifest acquisition contract.
- Provider-confirmed per-file identities are required before trial selection.
- The v27 proof tables are additive and **not** a download permit.
- The new proof writer requires an active fenced VERIFY_RELEASE job claim,
  event/torrent hash alignment and all regular video files to be accounted for.
- Unknown/ambiguous files and mixed seasons fail closed. A known OVA filename
  is only skipped for planning, not automatically deleted.
- Configured trial windows are read-only input; no worker queue, metainfo
  source, qBittorrent, staging or Sonarr workflow changed in this draft.
- Existing NEEDS_REVIEW rows remain untouched.

## Acceptance target

Shiki S01 + OVA: identify the anime, hash-verify torrent file inventory,
provider-verify each distinct regular episode, mark OVAs as skipped, record
durable per-file evidence, select the allowed first probe, prove JP+EN audio,
then download and stage remaining authorized episodes without selecting OVAs
or E07+ by accident (unless configured trial policy expressly includes them).

## Testing state

New focused regression tests were committed. GitHub CI was not started or
checked, and full repository tests were not run, per user request.
The assistant's current runtime cannot access the private repository
checkout to execute tests locally. Do not treat this as a tested/merge-ready PR.

## Follow-up draft PR #151 (2026-10-26): worker handoff and diagnostics

**Added but not merged:** A trusted make_batch_reader reads the pinned
40-character metainfo hash through the configured source in batch mode;
the existing single-episode metadata reader still rejects multi-video packs.
make_batch_verifier combines exact inventory with AniDB/Anime-Lists/TVDB
evidence, and make_verifier(..., batch_verifier=...) opt-in routes recognized
season packs to the batch verification path.

The VERIFY_RELEASE handler accepts typed batch completion, persists fenced
v27 per-file facts, and intentionally does not dispatch single-episode
EVALUATE_TRIAL for a multi-file pack. **Default worker composition now injects the batch verifier** while retaining
the single-episode path. Batch results do not yet trigger downloads.
Integration tests are committed but not executed here.

plan_stopped_batch_probe is a read-only preflight of owned, stopped qB state:
exact ordered files, marker, destination, category, policy budgets, and
piece-disjoint probe video. It produces intended priorities but does not
submit torrents, change priorities, or start downloads. A durable single-use
external-send authorization/reconciliation journal is still required.

The authenticated GET /api/v1/jobs/{id}/coverage and Episode coverage
dashboard action distinguish persisted provider episode facts from identity
matching and staging; response is bounded to the first 100 verified files.

### Outstanding milestones for end-to-end batch acquisition

- [x] Install trusted batch reader and verifier into actual worker composition (not yet merged).
- [ ] Add durable batch trial evaluation and episode reservations/deduplication.
- [ ] Journal qB stopped submission, selection, start and uncertain sends.
- [ ] Verify real first-file JP+EN audio before unlocking other files.
- [ ] Per-file verification, staging, Jellyfin and Keep/Reject safety.
- [ ] Durable detailed mapping/excluded-file diagnostics.
- [ ] Run local/full migration, Python and browser tests before next merge.
- [ ] Preserve separate RETRY PR #150 for the other chat (unmerged).

**Testing note:** The seven locally available pre-existing batch-planner
unit tests passed. New integration tests are committed but not run, and
no GitHub CI was manually run or inspected per the user's request.


## Follow-up PR #152 — durable batch trial selection (unmerged)

- [x] Add the EVALUATE_BATCH job for completed provider-backed season packs.
- [x] Reject packs with no independently verified TVDB E01 anchor.
- [x] Apply the installed backfill/following window and per-file/total budgets.
- [x] Add v28 additive tables recording immutable planned file indices,
      canonical episodes and sizes, under a fenced worker transaction.
- [x] Make a verified batch dispatch EVALUATE_BATCH instead of the single
      episode EVALUATE_TRIAL, and add the handler to default worker composition.
- [x] Extend Episode coverage with a separate planned/not-planned state.
- [x] Add focused v28 worker tests: complete Shiki pack, missing E01, and
      explicit absence of torrent or staging side effects.

### Still BLOCKING a functional batch download

- [ ] Harmonize multi-episode claims with all existing single-episode
      reservations, active/held duplicates, staged results and cancellations.
- [ ] Create a durable, one-shot stopped qB add and file-priority journal
      with reconciliation after ambiguous network failures and process crash.
- [ ] Authorize a probe file through the same operation ownership, size
      and piece-overlap checks; never enable unknown or OVA files.
- [ ] Persist real JP+EN audio verification for the downloaded probe and
      only then allow additional provider-confirmed files to download.
- [ ] Per-episode staging publications and Keep/Reject/cleanup, including
      Jellyfin integration and safe source ownership release.
- [ ] Full offline SQLite migration and fake-client worker crash tests,
      then full local regression suite before production deployment.

Plan rows are **read-only intentions, not episode reservations or download
authorization**. The presence of v28 schema and job registrations does not
make batch packs downloadable. RETRY remains isolated in PR #150.


## PR #152 checkpoint — additive v28/v29 batch trial and claim infrastructure

New code on this draft branch (not merged or deployed):

- [x] v28: durable E01-anchored trial plan, bounded backfill/following
  window, per-episode and total size budgets, planned-vs-verified dashboard.
- [x] v29: per-file batch reservations with partial active-claim indexes
  and a composite FK to each plan's selected file.
- [x] Atomic all-or-nothing batch claims; competing active *single* and
  *batch* owners are checked in the same SQLite BEGIN IMMEDIATE transaction.
- [x] Existing single-episode reservation writer reciprocally checks
  the batch claim namespace to prevent simultaneous episode ownership.
- [x] Cancellation cleanup can release canceled, drained batch claims.
- [x] Optional RESERVE_BATCH worker registration (not enabled by default).
- [x] Hash-bound, read-only acquisition candidate loader requires DONE
  reservation ownership, ACTIVE operation, exact episode/file/size proof,
  no same-hash competing active batch or single acquisition and
  non-rejected anime. It cannot add/select/start a torrent.
- [x] Regression test files for job handoff, missing E01, competing packs,
  hash drift and fresh/upgrade v29 migration parity and rollback.

IMPORTANT: Default worker registers EVALUATE_BATCH but does NOT yet
create RESERVE_BATCH. This deliberate cutoff prevents newly verified
season packs from holding episode slots while downloading is unavailable.

Still pending before enabling real batch media acquisition:

- [ ] Durable one-shot qB add / priority / start command journal with
  uncertain-send and crash/restart reconciliation.
- [ ] Re-fetched torrent metainfo binding and piece disjointness preflight
  at the actual guarded send transaction.
- [ ] Per-file JP+EN audio proof for probe; only unlock verified later files
  after durable proof, with a separate auditable permission.
- [ ] Per-episode download completion, staging, decisions, Jellyfin,
  ownership-aware Keep/Reject cleanup, and cancellation reconciliation.
- [ ] Full local migration/worker/transport tests and integration review.

No GitHub CI was manually started or inspected. Seven isolated planning
tests passed locally, plus v29 SQL constraint smoke tests. The new
repository integration tests were committed but could not be run using
a full local checkout. PR #150 (RETRY) is separate and unmerged.


## PR #152 continued — v30 stopped-only submission journal

- [x] Added v30 additive batch_torrent_submissions journal with one
  durable row per plan and globally unique infohash/ownership marker.
- [x] BatchSubmissionJournal verifies a RUNNING SUBMIT_BATCH lease, direct
  parent RESERVE_BATCH job and shared ACTIVE operation; rechecks
  hash-bound, provider-selected files and active reservations in the
  same SQLite transaction before authorizing a stopped add.
- [x] The first insert commits UNCERTAIN before returning a process-local
  BatchSendPermit. Repeated requests and worker restarts can only obtain
  the prior intent and must reconcile it; they receive no replacement
  permit, even if the torrent is absent or the network request timed out.
- [x] Read-only observation confirms exact stopped state, path, category,
  owner tag, autoTMM disabled and full ordered torrent file inventory.
  Unknown/changed provenance transitions to REVIEW rather than retrying.
- [x] Separate BatchClient.add_batch_stopped validates original hash-bound
  BEP3 bytes, consumes the one-use permit and sends ONLY stopped/paused
  torrent add, with no filePrio or start endpoint.
- [x] Existing cancellation release now refuses to free batch reservations
  if any batch_torrent_submissions row exists for the operation. A future
  stop/remove reconciler must safely drain that external resource first.
- [x] Committed offline tests for permit non-rearm, changed tags, exact
  stopped observation, metadata mutation, and v30 migration parity/rollback.

**Important:** SUBMIT_BATCH is intentionally not installed or routed from
the default worker. No batch add, priority change, restart/start, audio
verification or staging can occur in production. The journal and client
are building blocks, not a complete acquisition pipeline.

### Remaining gates before a runnable batch acquisition

- [ ] Implement an explicit SUBMIT_BATCH handler with typed, owned
  handoff and no retry of UNCERTAIN sends; test crash before/after add.
- [ ] Add a second durable one-use file-priority selection journal, piece
  overlap confirmation, and no-start-before-selected guarantee.
- [ ] Persist probe JP+EN audio evidence; only then journal an optional
  E02-E06 unlock with verified per-file selection.
- [ ] Add per-file download completion, staging publications, Jellyfin
  handling, Keep/Reject decisions, and bounded cleanup reconciliation.
- [ ] Validate all migrations and integrated worker/transfer tests
  locally before merging or deploying. GitHub CI remains disabled
  by user request. RETRY remains separate in unmerged PR #150.


## Latest checkpoint: v31–v33 probe, JP+EN audio proof and gated unlock

**Draft PR #152 only; NOT merged or deployed.** Earlier v28–v30 work
remains on this branch. Separate review-retry PR #150 is untouched.

### Implemented (new, opt-in infrastructure)

- [x] **v31** batch_torrent_transfers: durable one-shot E01-only
  file-priority selection, confirmed stopped snapshot, one-use torrent start,
  and read-only download progress reconciliation. An interrupted or unknown
  send never creates a replacement permit.
- [x] BatchClient.apply_probe_selection() and start_probe():
  validate exact hash-bound inventory, owned destination/category/tag,
  canonical piece boundaries and sole E01 priority before mutation.
- [x] **v32** batch_probe_media: typed immutable E01 file SHA-256,
  inode signature and ffprobe evidence requiring non-commentary Japanese
  and English audio. Fenced to a completed, owned transfer job.
- [x] Optional VERIFY_BATCH_PROBE reads the real file through
  O_NOFOLLOW descriptor-relative traversal, checks before/after stat,
  verifies full content hash and uses the bounded existing ffprobe parser.
- [x] **v33** batch_torrent_unlocks: second one-shot authorization
  journal. No later episode can become wanted until the actual E01 proof
  is present and matches the torrent/operation. Reconcile uncertain
  priority updates after restart; do not reissue them.
- [x] BatchClient.apply_audio_verified_unlock() and
  start_audio_verified_batch() only select provider-verified trial
  files, leaving OVAs and out-of-window episodes at zero priority.
  Reject wanted/unwanted shared pieces and wrong ownership before sending.
- [x] Optional SUBMIT_BATCH, TRANSFER_BATCH, VERIFY_BATCH_PROBE
  and UNLOCK_BATCH handlers with explicit job lineage, hash-only metainfo
  and separately guarded transfer/verification phases.
- [x] Handler display metadata and focused offline test code for one-use
  permits, wrong tags, shared pieces, altered metadata, excluded OVAs,
  schema parity and migration rollback.

### Deliberately NOT enabled

The **default NextGen worker registers only EVALUATE_BATCH** from the
batch pipeline. It does NOT dispatch RESERVE_BATCH, SUBMIT_BATCH,
TRANSFER_BATCH, VERIFY_BATCH_PROBE or UNLOCK_BATCH. Thus #152 cannot
start season-pack downloads, change qB priorities or move media in
production in its current configuration.

### Remaining essential integration

1. [ ] Durable per-file media-proof ledger for **every** planned
   episode after expanded download. Each later file must independently
   pass actual JP+EN ffprobe, content hash and inode checks.
2. [ ] One crash-safe staging publication per canonical episode,
   reusing existing Publisher and artifact receipts without weakening
   no-follow, ownership, atomic rename or exact destination episode IDs.
3. [ ] Extend Keep/Reject, duplicate staging protection, Jellyfin
   refresh, cancellation/cleanup and reservation release to those
   batch publication records. No broad directory deletion.
4. [ ] One-shot, crash-safe stopped-state transition after E01 completes.
   Unlock currently requires an already stopped torrent; it does not
   send an unjournaled stop command.
5. [ ] Full fake-provider/fake-qB, SQLite restart, filesystem/inode,
   staging/Keep/Reject and migration test execution in a full checkout.
6. [ ] Validate a Shiki S01 + OVA torrent in a controlled environment,
   proving no OVA, unknown or out-of-window video is acquired.

### Test policy and release status

No GitHub CI was started or inspected and no production deployment occurred.
New regression tests are committed, but the full repository suite was not
executed here. Do not merge #152 or enable new batch acquisition handlers
until per-file staging, cleanup and full integration tests pass.
Review-retry remains isolated in draft PR #150.


## Batch worker enablement follow-up — 2026-10-26

Feature branch: `feature/enable-nextgen-batch-acquisition-20261026`.

- [x] Register `RESERVE_BATCH`, `SUBMIT_BATCH`, `TRANSFER_BATCH`,
  `VERIFY_BATCH_PROBE` and `UNLOCK_BATCH` in the default provider
  worker, with a full successor chain from `EVALUATE_BATCH`.
- [x] Fix the `RESERVE_BATCH` completion and successor to send its **own
  reservation job ID**, never the upstream trial evaluation job ID.
- [x] Make enabled acquisition the default in `build_identity_worker`
  and `run_identity_worker`.
- [x] Complete the single default batch handler chain with no
  enable/disable option. (Earlier rollout-switch instructions are superseded.)
- [x] Add default-composition and successor-chain regression tests.

### Deployment and behavior WARNING

Enabling these handlers can now **reserve real episodes and submit or start
real qBittorrent downloads** after the verified batch trial. It is
NOT a complete, production-accepted batch media workflow.

The chain currently ends after `UNLOCK_BATCH` finishes. No per-episode
staging publication, Sonarr Keep/Reject, automatic source cleanup, or
Jellyfin refresh is wired for batch episodes. The E01->expanded-selection
transition also requires the torrent to be stopped, but a journaled
automatic stop step has **not** been added; it may remain waiting after
the E01 probe finishes unless safely stopped externally.

Any batch with an external submission journal intentionally retains its
active episode reservations during cancellation until resource cleanup
is explicitly reconciled. Do NOT manually clear reservations or delete
the batch database rows; that would break source ownership protection.

For a controlled rollout: back up the local SQLite database; ensure
v33 migrations succeeded; use a disposable test pack and isolated qB
download root; monitor the queued jobs, selected file priorities and
disk consumption. Return to the disable switch before processing new
packs if a state becomes uncertain. The disable switch does NOT stop
an already-owned, externally running qB torrent or clear previously
queued jobs; existing work needs explicit reconciliation.

The user asked not to trigger GitHub CI. New tests have been committed
but not executed against a complete repository checkout. No deployment
has been performed by the assistant. RETRY PR #150 remains unmerged.


## Current lifecycle branch — single default pipeline, v34–v36

Branch: feature/nextgen-batch-complete-lifecycle-20261026.
This is **not merged or deployed**.

- [x] Remove all temporary batch enable/disable parameters from the
  worker composition and remove the --no-batch-acquisition CLI option.
  The project must have one integrated batch path.
- [x] v34 batch_probe_stops: one-use E01 stop authorization and
  state reconciliation before changing file priorities. A restart
  cannot recreate a stop permit. After an expanded selection exists
  the worker never re-enters the E01-only stopped-probe path.
- [x] v35 batch_episode_media: independently verify JP+EN audio,
  full-file SHA-256, exact selected file, no-follow path and inode
  stability for each approved regular episode (not only E01).
- [x] v36 batch_staging_publications: per-episode pinned manifest,
  immutable no-replace artifact receipts, exact canonical AniDB/TVDB
  placement and duplicate-published-episode rejection.
- [x] Extend the existing staging publisher with COPY-only batch media,
  digest each copied destination file, preserve qB source files, and
  serialize sibling stage operations. The last published episode
  may mark the whole operation staged only once all selected
  episodes are published and preceding workers are complete.
- [x] Route UNLOCK_BATCH into individually fenced
  VERIFY_BATCH_EPISODE and STAGE_BATCH_EPISODE successors.
- [x] Make completed batch episode staging eligible to request a
  Jellyfin refresh without changing its separate worker scope.
- [x] Add filesystem copy/idempotency, stop-permit and v34–v36
  migration regression test source. Full tests are NOT yet run.

### Blocking completion before merge/deployment

- [ ] Integrate **batch Keep**: the current KEEP_MEDIA action and Sonarr
  keep-import journal operate on exactly one staged episode. A batch
  must reconcile Sonarr imports for *every* selected canonical episode
  with separate persisted per-file request/result phases.
- [ ] Integrate **batch Reject** with a bounded list of the operation's
  staged media and episode NFOs, respecting ownership and preserving
  shared series metadata. Avoid using the single-publication cleanup
  entry point for a multi-episode pack.
- [ ] Implement one-shot, crash-safe **batch qB cleanup** and exact
  recorded-file deletion only after confirmed removal and Keep/Reject,
  including canceled batches and reservation release. Never broadly
  delete torrent roots or free active reservations.
- [ ] Make dashboard/management action capabilities and completion
  status reflect all per-episode Keep/Reject/cleanup results.
- [ ] Run the whole Python 3.11–3.13 and browser suites, actual v33->v36
  migration/upgraded SQLite recovery, and simulated qB crash/restart
  transitions, including a real Shiki S01 + OVA acceptance pack.
  The user requested no GitHub CI runs; do not trigger CI.

**Important:** Batch acquisition is already default-enabled in merged
PR #153; this branch is a corrective implementation, not an optional
feature toggle. It must not be merged or deployed as a complete system
until Keep/Reject, cleanup, and full testing are solved.
