# Stage verified viewing media

New STAGE_USER_EVALUATION_MEDIA plans use **COPY**. The source remains in the
qBittorrent-owned `ad-<marker>` directory until staging is committed.
Persisted `transfer: MOVE` plans are kept as-is to recover older jobs
(including those whose media was already renamed into staging). Do not
rewrite a saved publication manifest or assume a legacy source still exists.

The worker reuses the existing journaled Publisher, the immutable manifest,
its job/operation ownership receipts and the staging-folder checks. The
selected video has already passed media verification, including its SHA-256.
A staging COPY streams the expected number of bytes, checks the destination
size and unchanged source size/mtime, fsyncs the file and atomically publishes
it. **Staging does not perform another full-file video hash or readback.**
This detects interrupted/incomplete transfers, not silent same-size corruption.

## Recovery

- On retry, a completed, journal-owned video is reused. Unknown existing
  video files are never overwritten or adopted.
- A registered incomplete COPY temporary is truncated and recreated from
  the still-present source. A registered uncertain temporary with the
  expected size has already been flushed and may be published.
- A missing registered COPY temporary is recreated from source. If a legacy
  MOVE left its only video copy missing, the job remains in review.
- Episode NFO files belonging to the publication, and shared series
  `tvshow.nfo` files, are generated from the pinned manifest. Missing
  or mismatched NFOs are recreated through the tracked temporary-file
  publication path, with the existing series-folder lock.
- Unknown episode NFOs without a matching publication receipt, symlinks,
  conflicting video destinations, changed verified source content, and
  unowned temporary files are not silently deleted or overwritten.

A complete publication is committed with the operation's STAGED transition.
Only then is an independent owned-download cleanup job queued. That cleanup
tells qBittorrent to delete its torrent and data and removes any remaining
pinned `ad-*` directory. A failed stage never queues source deletion.

Batch staging reuses the same Publisher COPY path. The same no-extra-video-
hashing policy applies to batch receipt verification.

The earlier MOVE-only publication/recovery contract is retained for
pre-upgrade manifests; new work does not move the source.

Irrecoverable staging integrity failures may attach the existing structured
`failed_check` and `failed_path` History fields, so an operator can see
which file required review. Successful NFO repair does not create a failure.
