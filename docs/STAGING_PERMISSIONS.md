# Anime-Trial staging ownership and group access

AniKeepOrNot's trial staging originally copied into a `tempfile.mkstemp`
file, which defaults to mode `0600`; hard-linking that inode into the
final `Season 01/S01E01.mkv` retained the private mode and prevented
Jellyfin from accessing the file through its shared media group.

Configure via **Settings → Staged media permissions**:

- **Staged file owner (optional)**: blank (recommended) retains the
  `anime-discovery` service account. A username or numeric UID may be
  supplied when the service is allowed to assign it. The service never
  elevates permissions or invokes sudo.
- **Staged file group (optional)**: blank retains the existing service
  group. A group name such as `media` or numeric GID such as `1002`
  assigns new MKVs and NFOs to the desired sharing group. The daemon
  must have that group in its supplementary list, or the storage must
  permit the change.

The permission policy now has **two independent four-digit octal settings**:

- **Directory mode** (default `2770`): applies to the staged series
  and season folders. Mode `2770` grants group **read/write/traverse**
  and enables setgid inheritance, so shared services such as Bazarr can
  create sidecar subtitles in a season directory when the NAS ACL permits.
  Mode `2775` adds traversal and reading for other users.
- **File mode** (default `0640`): applies to staged MKVs and generated
  NFO files. Group read permission allows Jellyfin to open videos without
  granting permission to modify them. Use `0660` only for workflows that
  really need to edit existing media in place.

Directories require owner rwx; files require owner rw. Four octal digits
are mandatory; unsafe special bits are rejected except directory setgid.
These are explicit settings, not an attempt to inspect or clone a NAS ACL.

The publisher copies media into a private temporary file, verifies its
checksum, then applies the configured file ownership/mode **before** the
file becomes visible via the final hard link. It verifies effective
ownership/mode after changing it. Both directory modes and NFO files
are handled explicitly. A permission failure is surfaced instead of
silently completing the acquisition.

The root trial mount and its ACLs must still grant appropriate traversal;
NFSv4 server ACLs can override local POSIX expectations. A successful
`chmod` and `chgrp` are verified by a readback on the client.

## Current media is not retroactively modified

Setting a new staged owner/group only applies to future staging. For
previously completed Solo Leveling episodes, inspect the metadata first:

```sh
stat -c '%a %u:%g %n' '/mnt/media/Anime-Trial/Solo Leveling/Season 01/'*.mkv
```

If the local NAS access model permits it, an administrator can separately
repair existing copies by changing their group to `media` and mode to
`0640`; confirm existing NFO permissions too. Do not rerun acquisition
or remove originals solely to fix permissions. Verify that Jellyfin itself
can read the file through its mounted NFS path.
