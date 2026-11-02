# Restartable owned-torrent cleanup and polling accounting

## Goal

A worker restart, crash, client restart or hours offline must not strand an
owned torrent at `STOP_UNCERTAIN`. A saved control intent is not proof that
qBittorrent received a request. Recovery re-observes current client state.

## Safety boundaries

1. The job must still own its lease and pass the operation gate. The same
   staging/decision, original acquisition plan, and sharing checks apply.
2. Every stop request revalidates the exact hash, marker tag, category, file
   manifest, download root and auto-TMM setting through `owned()`.
3. When already stopped, do not send another stop. When actively transferring,
   a stop request is repeatable (including from legacy `STOP_UNCERTAIN`).
   Checking/moving states are observed later; unexpected states need review.
4. `REMOVE_UNCERTAIN` deliberately remains *one-shot*: do not repeat the
   torrent removal command if an earlier response was lost. Observe absence
   before proceeding to protected file cleanup. The deletion uses
   `deleteFiles=false`, with local files handled under separate identity
   and directory safeguards.
5. Do not clear ownership records, force database states, silently adopt
   external torrents, or delete unknown files to make a job progress.

Cleanup still uses the existing 30-second deferred scheduling interval.
The qBittorrent adapter has bounded metadata polling and HTTP timeouts, but
not a universal request-rate limiter. No new polling loop is introduced.

## Lifecycle accounting (no schema migration)

The durable `jobs.attempt_count` continues to count scheduler claims. It
must remain unchanged for leases, audit history and API compare-and-swap
actions. It should **not** be presented as failed processing attempts:

- `poll_check_count`: job-history `DEFERRED` transitions, i.e. completed
  waiting observations. These are not failures and don't use the retry budget.
- `work_attempt_count`: total scheduler claims minus completed deferred
  checks. Includes a currently running or interrupted claim and retry/failure
  outcomes; it is **not** a count of failed requests.
- `scheduler_claim_count`: total persisted claims (`attempt_count`).

These values are computed when the operator opens a job's History, instead of
scanning all historical deferrals for every 5-second dashboard refresh.
History displays all three explicitly. The Activity/Jobs view also displays
the current cleanup checkpoint and when that checkpoint last advanced.

## Recovery examples

- Stop request delivered, response lost: read back stopped state; move on
  without a second stop request.
- Stop request not delivered, worker restarted: find exact owned active
  torrent; send stop again; verify and continue.
- Torrent checking: wait and observe later; do not send stop during checking.
- Uncertain removal with torrent still present: do not send another delete.
  Preserve data for a safe operator review/reconciliation design.
- Changed ownership/provenance: stop and require review; do not touch torrent.
- Long downtime alone does not consume a retry budget; only actual attempts
  after the service resumes count. Deferred checks remain separately visible.

## Operational verification

For a reported stuck cleanup, inspect `jobs.last_reason_code`,
`cancellation_cleanup.phase`, and the exact torrent state in qBittorrent.
The checkpoint is not a direct authorization to delete anything manually.
Tests cover lost stop responses, a still-active stop after simulated restart,
unchanged one-shot deletion, altered ownership, and the History count display.
