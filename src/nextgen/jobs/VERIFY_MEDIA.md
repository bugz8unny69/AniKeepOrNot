# Verify downloaded media

The default identity worker now atomically advances completed DOWNLOAD_TORRENT
work into VERIFY_MEDIA. Its payload binds the preparation and completed download
job IDs in the same acquisition operation. Existing custom download registrations
retain opt-in successor routing. A historical DONE download is not automatically
replayed or swept; it needs a separately authorized recovery policy.

The handler uses the immutable prepared inventory, episode declaration and
submission/transfer journals. It requires completed selection progress and the
exact submission/selection fingerprints. It reads only the selected MKV/MP4 under
the recorded per-acquisition `ad-<marker>` download folder. The worker needs the
same absolute shared filesystem path as the client: remote path existence or
client progress alone is insufficient. Staging/permanent roots cannot overlap
that download root.

Every absolute and relative directory component is opened without following
symlinks. Unknown files, nonregular files, hardlinks, size mismatch, changed
inode/timestamps or a replaced path cannot establish successful verification.
The selected file stays open while SHA-256 and ffprobe inspect it; final checks
reopen its absolute path and compare device/inode/size/mtime/ctime. The read loop
checks cancellation between one-MiB chunks. Blocking filesystem reads, including
NFS reads, may still delay drainage; they are not forcibly interrupted.

The fixed executable `/usr/bin/ffprobe` must be installed and executable by the
worker. No new package is installed automatically. It receives the already-open
file through `/proc/self/fd`, no shell, a file/pipe protocol allowlist and MKV/MP4
demuxer allowlist. Probe output is limited to one MiB, runtime to 60 seconds with
cancellation checks; termination always kills and reaps an unfinished process.
Stderr/raw probe data are discarded, never published as logs or history.

Require a recognized container matching the selected suffix, one video stream
with known codec/dimensions, finite positive duration and known-codec audio
streams tagged Japanese and English. `ja`/`jpn` and `en`/`eng` are recognized,
case-insensitively; comment-disposition tracks cannot satisfy a language.
Subtitles, filename claims and duplicate same-language tracks do not qualify.
Unknown/missing language metadata requires review. This inspects container and
stream metadata, not speech recognition, full-frame decoding, independent torrent
piece verification or semantic proof of which story/episode is in the video.
Episode association remains the existing verified provider mapping plus approved
filename declaration. Do not describe these checks as proof of perfect playback.

SQLite v19 adds immutable media_verifications with source job IDs, pinned journal
fingerprints, relative path, root/file identity, SHA-256, duration and language
facts. Completion rechecks current acquisition facts in the existing worker's
lease- and cancellation-fenced transaction. Evidence and DONE commit together;
a failed transaction publishes neither. Duplicate conflicting evidence is rejected.
Explicit preview/backup/maintenance upgrade is required from v18; startup never
upgrades an existing database. Configuration remains v7, secrets v3.

Missing/inaccessible files and probe timeouts use bounded worker retries. Missing
ffprobe enters readable review. Invalid files or missing audio also enter review;
all source files remain untouched. Existing cancellation/drain protection includes
verification, and reservation protection stays in place after verification.

No staging job is installed yet. A completed verification is not STAGED, does not
authorize Keep, publish NFO files or request Jellyfin refresh. Staging must recheck
the recorded source signature/digest before publishing a copy, recover through
its own durable publication intent and never overwrite unrelated files.

Offline cases cover byte/signature changes, path replacement, symlinks/hardlinks,
FIFOs, unknown files, audio tags, malformed/oversized probe responses, bounded
process cleanup, worker handoff, operation mismatch, lease loss, cancellation and
schema upgrades. All tests and CI remain unrun per project policy. No live media,
download client, provider, migration, service restart or deployment was used.

## Staging follow-up

The default composition now supplies STAGE_USER_EVALUATION_MEDIA as an optional successor.
The worker commits verification evidence, DONE and the successor together. See
[STAGE_USER_EVALUATION_MEDIA.md](STAGE_USER_EVALUATION_MEDIA.md). Custom registrations can omit it;
verification alone still never marks STAGED or authorizes Keep. Earlier scope
notes describe the prior implementation stage.
