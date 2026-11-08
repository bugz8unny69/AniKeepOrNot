# Sonarr magnet acquisition: implementation checkpoint

This feature branch is **not ready for deployment or merge** until CI and the
real Sonarr-to-qBittorrent staging flow are verified. Issue #167 tracks the
feature, and PR #168 targets `NextGen` for review and automated CI.

## Implemented in this checkpoint

- A terminally failed Sonarr acquisition now submits the existing
  `CLEANUP_CANCELED_OPERATION` worker automatically, in the failing job's
  transaction. It does not require the operator to press Cancel. Other jobs
  and unrelated episodes are unaffected. A **manual** Cancel stays final and
  does **not** advance ranked candidates.
- The legacy magnet wait is bounded to ten minutes using the original durable
  intent timestamp, not a per-attempt reset. After timeout the same cleanup
  worker reconciles qBittorrent. If the magnet is absent and its isolated
  destination is empty, the attempt can retire; if the original owned
  magnet has no files, its one-shot removal uses `deleteFiles=false`.
  Ownership conflicts or unknown local files still require review.
- Ranked fallback now runs automatically after cleanup and reservation release.
  If the next ranked release is missing from a successful Sonarr search,
  the same worker records it as unavailable and advances to the next stored
  rank; transport failures retry that rank. A final failure schedules an
  existing discovery job that reports `SONARR_CANDIDATES_EXHAUSTED` for review.
  No new worker or table is added.
- New offline regression tests cover automatic cleanup, manual cancellation,
  exhausted ranks, magnet metadata timeout and metadata-less cleanup.
  **None of these tests have been executed, by request.**

- The version-specific qBittorrent adapter now reuses the stopped magnet
  admission path for 4.2.x, newer 4.x, and 5.x versions without metadata-only
  fetch. A running 4.2.x magnet can be recovered after upgrading to 5.x,
  including 5.2+; the original submission marker stays valid and no second
  magnet add is authorized.
- Sonarr deliveries now use an independent existing operation per candidate.
  Canceling one candidate no longer cancels the backfill worker or siblings.
- After a *reviewed* candidate is canceled, its cleanup completes and its
  reservation is released, the existing ranked candidate records advance and
  schedule another `DISCOVER_SONARR_BACKFILL` job. That worker re-fetches
  Sonarr's next ranked magnet and sends it through the normal verification
  pipeline. This uses existing tables and existing worker services.
- Regression tests for the upgrade path, isolated operations and fallback
  scheduling/selection have been written but deliberately **not executed**.

- The installed `python -m src.anime worker` entry point now accepts
  `--sonarr-acquire`. It enables the existing backfill, source selection,
  and acquisition registrations together. The example NextGen systemd worker
  unit now passes the flag so a deployed worker activates that existing path.
- The 4.2.5 adapter now permits up to 20 one-second metadata polls during
  each owned resume/pause window instead of the former ~2-second window.
  This improves slow metadata discovery but is not live-daemon validation.
- Old-API tagging after a successful add uses one bounded readback routine for
  ordinary and pre-manifest admissions. It rejects another destination, category,
  automatic-management mode, active state, or existing tags.
- `MagnetPermit` is a compatibility alias for the existing one-use `SendPermit`.
- Start/stop endpoints use negotiated Web API capabilities throughout transfer
  and cleanup, rather than the application major version.
- A verified, stable stopped inventory can complete PREPARE_ACQUISITION. The
  completion transaction publishes the acquisition plan and original stopped
  submission receipt together, retaining the original marker and destination.
  SUBMIT_TORRENT then reconciles the original torrent instead of sending again.
- Observed inventory is a distinct evidence type. Migration 46 permits a NULL
  metainfo SHA256 in the existing acquisition table. No metainfo bytes or hashes
  are synthesized. Deserialization checks the explicit evidence discriminator.
- Observed inventories cannot authorize a fresh submission. Promotion requires
  the durable, exact-owned v45 admission and its matching captured inventory.
- Promoted admissions use normal inventory-bounded cancellation cleanup.
  Unpromoted admissions continue to retain reservations and require review.

Published migrations 1–45 remain unchanged. Migration 46 rebuilds the existing
acquisition table and preserves its rows, references, and indexes. There is no
new persistent table or worker service.

## Remaining work

1. **Verification deferred by operator request.** Run the written automated
   tests and one full Sonarr → qBittorrent 4.2.5 → stage integration scenario
   when permitted. Repeat with newer qBittorrent API versions, then CI.
2. **Ambiguous ownership still requires review.** An unpromoted torrent whose
   files are unknown, whose marker/path differs, or whose removal is uncertain
   is not deleted or silently retried. This is an intentional stop, not an
   automatic candidate fallback.
3. No journal consolidation or other architectural work is required for the
   implemented path. Published migrations 1–45 are unchanged.

## Verification scope

Offline tests exercise transport responses, one-use permits, inventory evidence,
SQLite migration parity/rollback, and worker completion handoff. They do not
establish live qBittorrent compatibility or complete Sonarr-to-staged-media
behavior. The 4.2.5 source at release commit
`2c65b796401f924d6889aadde958de3d2846bb4b` was inspected: file rows have positional
indexes and piece ranges; add accepts paused/autoTMM but has no inline tags.
This source inspection is not a live daemon test.

The local runtime denies AF_UNIX socket creation, affecting the admin socket
and systemd notification tests. Preserve those tests; do not skip or weaken them.
PR #168 is now open for the NextGen CI workflow. The code and offline tests
have not been verified by a completed CI run as of this checkpoint; any
failures should be handled as specific regression fixes, not new scope.

## Backfill policy correction (2026-10-29)

The former automatic series-registration and two-minute waiting policy was
incorrect for the intended search-only acquisition flow and has been removed.
Backfill search has since moved from Sonarr to Prowlarr. See
[the authoritative backfill and continue behavior](BACKFILL_BEHAVIOR.md).
The separate explicit Keep-in-Sonarr workflow is outside this change.
