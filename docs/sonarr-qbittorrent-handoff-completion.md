# Sonarr → qBittorrent handoff: completion checklist

Current automatic acquisition behavior: [Backfill and continue](BACKFILL_BEHAVIOR.md).
The historical checklist below does not authorize automatic series registration.

This document is the scope and progress record for **one** follow-up pull request
against `NextGen`. Keep it simple: use existing NextGen workers, jobs, tables,
and adapters. Do not create a separate framework or split this work across PRs.

## Existing implementation (already merged)

PR #168 merged Sonarr ranked-candidate acquisition, magnet admission through
the qBittorrent adapter (including 4.2.x metadata handling), recovery without
duplicate submission, automatic cleanup after terminal candidate failure, and
ranked fallback after cleanup and reservation release.

The normal `python -m src.anime worker --config-dir ...` startup
already enables Sonarr backfill, selection and acquisition. The sample systemd
unit uses this command without a feature flag. The obsolete hidden
`--sonarr-acquire` switch has been removed in this PR. The implementation
checkpoint is `docs/nextgen-sonarr-acquisition-status.md`.

**Implemented does not mean verified.** That checkpoint says its offline
regression tests were written but not executed; it does not establish a
successful end-to-end acquisition.

## Complete in this PR

- [x] Static code review: normal worker startup registers Sonarr discovery,
  selection and acquisition without a flag; sample systemd unit matches.
  Actual deployment and runtime behavior remain unverified.
- [ ] Run the existing **offline** Sonarr → qBittorrent tests; repair actual
  failures, without adding extra architecture.
- [ ] Verify offline that a ranked Sonarr candidate is selected, handed to the
  qBittorrent adapter once, and advanced to acquisition/staging on success.
- [ ] Verify offline failed candidate cleanup and next-ranked fallback; manual
  Cancel must remain final, not trigger fallback.
- [ ] Verify offline qBittorrent 4.2.5 versus newer Web API behavior, including
  paused/stopped magnet admission and metadata polling.
- [ ] Run the relevant NextGen CI checks and fix only failures related to this
  path. Report unrelated failures separately rather than expanding scope.
- [ ] Record exactly what passed, what failed, and any remaining blocker in the
  PR description before requesting merge.

## Out of scope

- No homelab live end-to-end test in this PR, as requested by the operator.
- No unrelated refactoring, new worker/table abstractions, or branch cleanup.
- Do not claim deployment readiness solely from offline test success.

**Goal:** a single reviewable PR showing which Sonarr → qBittorrent handoff
behaviors are implemented and which have actually passed offline tests.
