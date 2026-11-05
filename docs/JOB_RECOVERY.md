# Job Recovery Tooling (`adm recover`)

CLI tool for historical stopped job triage, classification, dry-run preview, and safe retry execution.

```bash
# 1. Stopped jobs overview and recovery summary
sudo adm recover summary [--config-dir /etc/anime-discovery] [--json]

# 2. List individual stopped jobs with recovery eligibility
sudo adm recover list [--reason REASON] [--kind KIND] [--eligible-only] [--manual-only] [--limit 50] [--json]

# 3. Dry-run preview of recovery action (No database modifications)
sudo adm recover preview --job-id 1292
sudo adm recover preview --reason TORRENT_METADATA_TIMEOUT [--limit 20]
sudo adm recover preview --all-eligible [--limit 50]

# 4. Apply recovery (mutating action; preview first if needed)
sudo adm recover apply --job-id 1292
sudo adm recover apply --reason TORRENT_METADATA_TIMEOUT
sudo adm recover apply --all-eligible --limit 50
```

---

## Safety Model & Guarantees

1. **Read-Only Preview**:
   Running `adm recover` or `adm recover preview` will never mutate the database, claim leases, or enqueue background jobs.
2. **Explicit Apply Subcommand**:
   `adm recover apply` is the mutating action; no separate confirmation flag is needed. The former `--apply` flag has been removed and is rejected.
3. **No Ad-Hoc SQL or Attempt Reset**:
   Recovery never executes raw `UPDATE jobs SET state='PENDING'` or clears attempt counts. It invokes the vetted `request_review_retry()` engine.
4. **Compare-And-Swap (CAS) Protection**:
   `request_review_retry()` checks that `attempt_count` and `last_reason_code` match the snapshot taken during inspection. If another worker or operator touches the job concurrently, the operation fails safely (`RetryStale`).
5. **Automatic Pre-recovery Backup**:
   `apply` automatically creates a consistent SQLite hot backup (e.g. `<db>_backup_<timestamp>.sqlite` with permissions `0600`) before running any mutations, unless `--no-backup` is specified.
6. **Bounded Execution**:
   Batch recovery is bounded (default 50, maximum 100 per invocation) to prevent worker lease storms.
7. **Maintenance Coordination**:
   All mutations run within `MaintenanceGate.operation()` so they never conflict with active schema upgrades.

---

## Recovery Matrix (17 Baseline Scenarios)

The tool classifies stopped jobs into categories based on whether code remediation has already enabled safe automatic retry:

| Category | Reason Codes | Action Taken |
|---|---|---|
| **Autonomous Retry** | `TORRENT_METADATA_TIMEOUT`, `ACQUISITION_SIZE_LIMIT`, `TORRENT_PIECE_OVERLAP`, `KEEP_IMPORT_REVIEW`, `STAGING_REVIEW`, `IDENTITY_INVALID`, `PROWLARR_SEARCH_REVIEW`, `SONARR_CANDIDATES_EXHAUSTED`, `WAITING_FOR_DUB`, `RELEASE_EPISODE_MAPPING_MISSING`, `MEDIA_AUDIO_REQUIRED` | Eligible for `--all-eligible` retry. Requeues to `PENDING` so worker activates candidate fallback or proceeds with fixed logic. |
| **Manual Action Required** | `IDENTITY_INCOMPLETE`, `IDENTITY_CONFLICT`, `RELEASE_TYPE_UNSUPPORTED`, `CANCELLATION_REVIEW`, `DECISION_DOWNLOAD_CLEANUP_REVIEW`, `TORRENT_METADATA_CONFIGURATION_INVALID` | Excluded from `--all-eligible`. Requires operator manual matching or inspection via `sudo adm report job --id N`. Retryable jobs may be targeted with `--job-id N` or `--reason CODE` once resolved. |
| **In Flight / Blocked** | `EPISODE_RESERVED`, active lease running, cancellation draining | Retrying blocked until active dependencies resolve. |

### Explicit operator selection versus automatic recovery

`--all-eligible` remains restricted to reasons classified `AUTONOMOUS_RETRY`.
A specific `--reason` or `--job-id` is an operator-selected retry and **does not**
require the reason to appear in the autonomous matrix. For example, after
correcting qBittorrent configuration:

```bash
sudo adm recover preview --reason TORRENT_METADATA_CONFIGURATION_INVALID
sudo adm recover apply --reason TORRENT_METADATA_CONFIGURATION_INVALID
```

This overrides **only the recovery matrix's recommendation**. The underlying
`capability()` and `request_review_retry()` safeguards still reject active leases,
unsupported kinds/states, inactive operations, already-handed-off jobs, and
stale attempts or reasons. No `--force`, direct SQL reset, or worker bypass is
introduced. Requeued jobs may return to review if the configuration remains
invalid.

---

## Bulk Management Jobs Behavior

Management API bulk orchestration jobs (`BULK_MANAGE_JOBS`) never stall in `NEEDS_REVIEW`. If any or all bulk operations are rejected, the bulk job cancels cleanly with reason `BULK_MANAGEMENT_PARTIAL` (or `JOB_PAYLOAD_INVALID`), leaving individual target jobs in their durable states.
