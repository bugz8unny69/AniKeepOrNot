# One-time settings fix

Stop the old service, then run:

```sh
sudo python3 fixes/migrate_legacy_settings.py
```

It backs up the original settings and credentials under a private `.settings-backup-*` directory inside `/etc/anime-discovery`, then converts those two files in place. Compatible values and file ownership are retained. A repeat run validates the converted configuration and does nothing. Interrupted writes are recovered from the backup on the next run.

No database is read or changed. The new application database location is recorded automatically. The old administrator password remains in the backup; use ADM to set the new password. Unsupported settings are reported by name and remain in the backup.

Run the installer normally afterward. No manual directory moves are needed. `--config-dir` is available only for installations using another location.


## One-time cleanup of obsolete Sonarr backfill results

Previous Sonarr discovery sometimes saved NOT_LISTED for episodes that had never
been searched using Prowlarr. The series Intake page cannot offer Retry for that
obsolete status. This separate one-time repair clears only unclaimed NOT_LISTED
*result rows*. Intake then reports those episodes as Not searched and offers
the existing Prowlarr Search action. It does not rewrite history, migrate the
schema, re-run discovery, or remove acquired/staged episodes.

After merging this fix into NextGen and running anime-discovery-install --upgrade,
first preview (read-only, services may be running):

```sh
sudo python3 /opt/anime-discovery/current/fixes/cleanup_legacy_not_listed.py
```


Review the eligible and protected counts. If the result is as expected, stop
both services, apply (which creates a private 0600 SQLite backup alongside the
database), and start the services again:

```sh
sudo systemctl stop anime-discovery-worker.service anime-discovery-api.service
sudo python3 /opt/anime-discovery/current/fixes/cleanup_legacy_not_listed.py --apply
sudo systemctl start anime-discovery-api.service anime-discovery-worker.service
```


Only rows without an active reservation or saved candidate/selection are
removed. Protected rows are reported and left unchanged. A second application
does nothing. The script requires the NextGen v48 database layout. If it
refuses, inspect the reported reason rather than forcing changes.


## One-time removal of torrents for already staged episodes

Since PR #204, staging removes the episode's torrent from qBittorrent (the
video was already moved into staging). Episodes staged before that still have
their torrent until Keep or Reject. This queues the same cleanup for them.
Episodes with a Keep/Reject decision, or whose cleanup already started, are
left alone. Staged videos and the library are never touched.

After merging #204 and running anime-discovery-install --upgrade, preview
(read-only):

```sh
sudo -u anime-discovery python3 /opt/anime-discovery/current/fixes/remove_staged_torrents.py
```

Then queue the cleanups (backs up the database first; services can stay running):

```sh
sudo -u anime-discovery python3 /opt/anime-discovery/current/fixes/remove_staged_torrents.py --apply
```

Each queued job appears in the dashboard as "Remove the finished download from
qBittorrent". A second run does nothing. Do not remove these torrents by hand.

## Repair Jellyfin NFO titles for existing staging

After installing the title-language update, use the existing title backfill:
it now corrects the <title> of published `tvshow.nfo` files and the
<showtitle> of episode NFOs, in addition to retrieving verified AniDB titles.
It retains canonical filenames and all episode/provider IDs.

Preview:

```sh
sudo -u anime-discovery python3 /opt/anime-discovery/current/fixes/backfill_anime_titles.py --dry-run
```

Apply (requires draining running NextGen work; automatically requests exclusive
maintenance and backs up SQLite before changing metadata):

```sh
sudo -u anime-discovery python3 /opt/anime-discovery/current/fixes/backfill_anime_titles.py --apply
```

The script updates existing publication proofs together with any changed NFO
files so subsequent Keep/Reject cleanup still works. Absent episode NFOs
after Keep are skipped, never recreated. A changed/unrecognized NFO is refused
rather than overwritten. After applying, it reuses the standard Jellyfin
`POST /Library/Refresh` scan request when configured. A 204 response only
means the scan was accepted, not that every Jellyfin item has reloaded its
metadata. NFOs already removed by post-Keep cleanup cannot be updated by this
script. If exclusive maintenance is unavailable or a repair fails, investigate
the pending maintenance state before restarting workers.

## Move audio-stuck backfill jobs to "Waiting for dub"

When English audio is required and every release of an episode failed only the
audio check, backfill now waits for a dub (job On hold, Intake shows
"Waiting for dub" with a Search again button) instead of stopping in Needs
review. This retries the backfill jobs that stopped in review that way before
the update. Each one searches Prowlarr once more: a new Dual/Multi Audio
release is tried normally, otherwise the job shows "Waiting for dub". Jobs
that failed for any other reason stay in review. Nothing is downloaded or
deleted by the script itself.

After merging and running anime-discovery-install --upgrade, preview
(read-only):

```sh
sudo -u anime-discovery python3 /opt/anime-discovery/current/fixes/requeue_waiting_for_dub.py
```

Then apply (backs up the database first; services can stay running):

```sh
sudo -u anime-discovery python3 /opt/anime-discovery/current/fixes/requeue_waiting_for_dub.py --apply
```

It refuses to apply when the Audio check setting does not require English. A
second run does nothing.

## NFS mount identity and title backfill

NextGen no longer requires persisted device/inode numbers to match when
repairing NFO titles or cleaning up already-staged files. A verified
publication path and the NFO's saved content hash identify the expected NFO;
large videos use inexpensive size/mtime checks in the ordinary case. Staging
retries similarly do not block solely because NFS reports new device/inode
values after remounting. The published ownership records and symlink-safe path
traversal are retained.

After installing this update, run the documented title-backfill dry run first.
Do not use `--apply` if it reports a genuine content/hash mismatch or a
foreign edited NFO. Successful `--apply` still backs up the database and
holds exclusive maintenance while files and receipts are updated.
