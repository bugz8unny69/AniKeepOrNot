# qBittorrent ownership and Keep cleanup

AniKeepOrNot deduplicates qBittorrent submissions by the **exact info hash** from
the webhook. It never uses title similarity or category names to decide that a
torrent belongs to it. For newly submitted torrents it creates (if necessary)
the fixed qBittorrent category `anime-discovery` and submits a unique tag
(`ad-<32 lowercase hex digits>`). The tag is stored in the application SQLite
database **before** the add request, and ownership is confirmed only when an
exact-hash inventory response reports both this category and the matching tag.

The provenance states are:

- `OWNED`: observed as an AniKeepOrNot add with the random marker, and
  read back from the qBittorrent inventory
- `EXTERNAL`: the exact hash already existed before AniKeepOrNot added
  anything; the application never retags or recategorizes that existing torrent
- `UNCERTAIN`: legacy torrent, missing/changed marker, or unverified result
  after a failed, interrupted, or raced submission

Unknown/missing database records are `UNCERTAIN`, not `OWNED`.
qBittorrent inventory is read back once more immediately before any final
destructive cleanup. If the tag/category has changed, the torrent is preserved.

## Keep-time notification

Clicking **Keep in Sonarr** starts the verified Sonarr copy/import process.
It does not delete torrents at that time. If any staged episode has external
or uncertain qBittorrent ownership, the series detail page immediately shows
a persistent **Original qBittorrent files preserved** message, including
the affected info hashes. This is a dashboard notice, not NTFY.

When the Sonarr import monitor later verifies all copies, it may remove
**only proven-owned** torrents. For external or uncertain torrents it leaves
the qBittorrent torrent and all its original files in place, and still completes
Keep. The temporary trial copies can be removed after successful verification;
those are not original qBittorrent files. The notice remains in the series
history after Keep completes. Manually delete the preserved torrents and files
in qBittorrent when you are ready.

Old installs have no ownership proofs for torrents submitted by earlier builds.
Those torrents are preserved conservatively and will show the notice at Keep.
Deleting them automatically without original proof would be unsafe.

The opt-in acquisition worker no longer removes torrent sources before Keep,
regardless of the historic `qbittorrent.cleanup_source` setting. This ensures
that the user-approved Sonarr Keep completion is the sole background
qBittorrent source cleanup boundary. It does not change how downloads are
checked for completion or the Keep→Sonarr→Shoko identity safety gates.
