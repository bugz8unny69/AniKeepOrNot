# Metadata retry forensics (NextGen)

## Scope

This change **does not alter retry policies, torrent admission, cleanup, ownership,
submission, start/stop operations or fallback selection**. It adds a bounded
per-attempt observation to the existing `DEFERRED` job-history row when
`VERIFY_RELEASE` awaits qBittorrent metadata.

`TORRENT_METADATA_WAITING` is a deferral, not an automatic retry failure. Its
attempt count can grow without consuming the worker failure-retry budget.

## What each new deferred attempt records

The `metadata_evidence` object in `job_history.details_json` contains:

| Field | Meaning |
|---|---|
| `phase` | `fetch`, `not_found`, `poll`, `recover_stop`, or `stopped_inspect` |
| `initial_state` | qBittorrent state before this attempt's discovery |
| `last_active_state` | Last polled qBittorrent state before stopping |
| `stopped_state` | State of the accepted stop observation, or not observed |
| `polls` | Polls observed in this attempt, not lifetime retries |
| `max_files` | Maximum number of file entries observed during this attempt, not a verified manifest |
| `resumed` | Whether this attempt sent a start/resume command |

No magnets, tracker URLs, raw API responses, peer addresses, credentials, save
paths, or release titles are stored in the new diagnostic object. Inputs are
bounded and validated before insertion into durable history. The journal's
`UNCERTAIN` state remains a safe default until a stopped, owned, stable
inventory has been independently verified.

## How to use it

After deploying this change and the worker has completed another metadata
waiting attempt, run:

```bash
sudo adm report job --id 691
sudo adm report job --id 692
```

The `DEFERRED` event now includes `metadata observation (this attempt)`.
Interpretation:

- `phase=poll`, `last_active=metaDL`, `max_files=0`: no file list was
  **observed by the polling loop**. This is not proof no peer was reachable.
- `phase=recover_stop`: previous run was still active when checked; worker
  recovered the stop before considering a new discovery window.
- `max_files>0` but no `OWNED_STOPPED`: inspect stable stopped readback and
  journal acceptance; a transient file list is not sufficient.
- `phase=not_found`: the owned torrent was not visible to the qB API when
  inspected.
- `phase=fetch`: metadata-only fetch returned pending on supported newer qB.

The report now distinguishes metadata admission from downstream downloading.
Historical reason messages and source excerpts are always resolved from the
**current** installed release; the old executing revision and original
unsanitized webhook are not reliably preserved in historical events.
Existing `DEFERRED` events cannot be reconstructed retroactively.

## Provenance and next investigation

The original payload is normalized before persistence. A missing stored
`magnet_uri` does not prove the sender omitted a magnet; it may have been
rejected during normalization. Do not infer original request fields or code
revision from the normalized record.

The current changes capture per-attempt observations, not the executing Git
commit SHA. A future independent installer/worker versioning change can
persist an exact immutable revision per invocation. Until then, report
historical source locations as **current-source interpretations**.

## Regression checks

Offline tests cover evidence bounds and redaction, a simulated qB 4.5 polling
window, job-history encoding, and report rendering. No real torrent needs to
be added, resumed, stopped, or deleted to run them.
