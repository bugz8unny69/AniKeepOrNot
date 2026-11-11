# Series actions, English folders and Reset Acquisitions

## What happens when you press Reset Acquisitions?

This is a **series-scoped replay of saved verified Autobrr intake**, not a
new discovery search. AniKeepOrNot already persists the original
`notifications` row, the torrent infohash and magnet in
`daemon_jobs.payload_json`, the TVDB/AniDB identity and episode approval.

On the **Series → [anime] → Manage series** panel, press
**Reset acquisitions & retry** and confirm. The server requires the same
authenticated session and CSRF token as other administrative actions.

The reset checks the currently verified identity and exact saved torrent
hash, confirms all selected series' acquisition jobs and dispatches are
`DONE`, and rejects active jobs, rejected/quarantined series, or any
series already handed off to Sonarr. It checks recorded media checksums
and paths before removing the *tracked*, verified Anime-Trial staged
copies/NFOs. Missing copies are okay if an operator already emptied the
trial directory. Unexpected contents and paths outside the trial root
cause a failure instead of deletion.

Within one SQLite write-locked operation it removes the **previous
completed acquisition job records for that series**, including their
old job-action events, clears only those episodes' recovery records,
sets dispatches back to `CLAIMED`, and creates new `READY`
`claimed_acquisition` jobs from the saved payloads. A durable
`series_acquisition_resets` audit entry records the old and new job
IDs. No other anime is reset.

**Preserved:** existing Autobrr notifications, verified episode and
series identity, approval evidence, original qBittorrent torrents,
qBittorrent ownership marker records and other series. The job may
reuse the existing torrent under its exact infohash when the worker
restarts; no new Autobrr webhook is needed. A reset does not wipe
the entire Anime-Trial root or untracked files there.

A failed or interrupted file removal can leave fewer staged copies
than the old `DONE` records expect, but the reset remains repeatable
and accepts already-missing files. Take a SQLite backup before using
this operation on valuable data.

## Understanding the other actions

- **Keep in Sonarr** — begin verified Sonarr handoff.
- **Hide old activity** (former Clear history) — adjust the visibility
  cutoff for previous notifications; does not remove jobs or files.
- **Remove rejection / quarantine** (former Reset) — restore series
  eligibility, without rerunning prior episodes or deleting media.
- **Reject series** — block further acquisitions until rejection is removed.
- **Retry Now** (Jobs page) — bypass the backoff timer **only for a
  deferred job**; already completed jobs need Reset Acquisitions instead.

## English staging folders

Episodes are staged under:

```text
Anime-Trial/
  Solo Leveling/
    Season 01/
      S01E01.mkv
      S01E02.mkv
```

The directory comes from TVDB's verified English translation or a
unique, TVDB-ID-matched recognized-name mapping. When neither is
available, the code accepts only an already-clean canonical stored
series title; release-group labels, codec markers, episode codes and
raw torrent filenames are not acceptable names. If no trustworthy
name exists, staging defers rather than generating a misleading folder.

TVDB identities remain stored in SQLite and `tvshow.nfo`. The same
destination builder is used for new staging and interrupted worker
reconciliation; paths and NFO identities are checked before reuse.

**Deployment order:** deploy the fixed code before resetting and
retrying an old acquisition. The reset is unavailable until the
new code is deployed, and the old worker would otherwise restage to the
obsolete TVDB-number directory.
