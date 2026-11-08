# Torrent metadata — qBittorrent protocol adapter

No external torrent metadata HTTP server is required. **Configuration schema v12**
retires both `torrent_metadata.base_url` and `torrent_metadata.timeout_seconds`.
The explicit v11 → v12 migration validates the historical settings and removes
that section. qBittorrent address, credentials and download folder remain
under the existing qBittorrent configuration.

## Supported qBittorrent metadata methods

| qBittorrent Web API | Method | Evidence |
| --- | --- | --- |
| 2.11.9+ (typically 5.2+) | Metadata-only fetch, no torrent add | Hash-verified BEP3 `.torrent` and `BatchInventory` |
| 2.8.14+ (older 4.x and 5.x) | Journaled stopped magnet, then `torrents/export` | Hash-verified BEP3 `.torrent` and `BatchInventory` |
| Earlier supported 4.2-era API | Journaled stopped magnet, bounded metadata discovery, double-checked `torrents/files` | `ObservedBatchInventory` tied to a journal-owned stopped torrent; **no invented BEP3 SHA** |

Use Web API capabilities, not the application version number alone.

## Legacy early batch admission and one-torrent lifecycle

1. Only a leased `VERIFY_RELEASE` job may request an early admission. A
   unique random `ad-<marker>`, destination `download_root/ad-<marker>`,
   category, client, job and exact infohash are recorded in
   `batch_metadata_admissions` **before the first add**.
2. Only that new journal row yields a process-local, one-use permit. The qB
   protocol adapter sends the event's sanitized, hash-bound magnet **paused or
   stopped**. Older APIs receive the tag immediately after the permitted add.
   Crash/retry may inspect and recover the intent but **cannot send again**.
3. qBittorrent may briefly resume an *owned* stopped magnet to discover its
   metadata, using the existing bounded legacy adapter and unconditional stop.
   Stable double-read, infohash, unique tag, isolated save path, category,
   autoTMM=false and stopped state are required before the inventory is saved.
4. For API 2.8.14+, `torrents/export` adds cryptographic BEP3 evidence
   (SHA-1 infohash and SHA-256 of the original info dictionary). On the
   oldest APIs, the original bencoded info is unavailable. Preserve the
   observed file-list evidence as a distinct type, with SQL `info_sha256=NULL`.
5. Every batch file still needs individual AniDB → Anime-Lists → TVDB episode
   corroboration, planning and reservations. Neither form of metadata
   authorizes a download by itself.
6. At `SUBMIT_BATCH`, the journal atomically **adopts the same stopped torrent**
   into the regular batch submission ledger, using the same marker and
   download root. The worker does **not add it again**. It rechecks ownership
   and file inventory before proceeding to the existing controlled E01 probe,
   media verification, unlock, transfer and staging steps.
7. After adoption the torrent may legitimately be active. Later metadata
   checks remain read-only, validate the unique owner tag and immutable files,
   but do not stop/restart an active download. The transfer journals separately
   authorize and verify all state-changing operations.
8. Cancellation of a *non-adopted* early magnet uses the existing exclusive
   operation-drain gate and a one-shot cleanup intent, guarding the same
   isolated root. Uncertain destructive commands are never blindly repeated.
   After release, a future job can reuse that infohash; the database's
   partial unique index applies only to live admissions. Adopted torrents
   use the existing batch cancellation/cleanup journal.

Existing unrelated torrents are **never adopted or deleted**, even if their
hashes match. A marker by itself is not proof of ownership. Crash/retry and
foreign/changed-state cases fail closed rather than issuing duplicate adds.

## Deployment and verification

- Preview and apply the **database v50 → v51** migration under maintenance
  with a backup, and migrate the settings document from v11 to v12 before
  restarting an updated worker. Keep the database and worker versions aligned.
- Offline tests cover the qB API capability matrix, single-send admission and
  replay, observed-vs-BEP3 inventory distinction, foreign torrent checks,
  active-adopted readback, provider episode proof, and both migrations.
- The full installed-system integration tests and GitHub CI must complete
  before merging or deploying this change. No production torrent behavior
  should be claimed verified until exercised against real supported clients.
