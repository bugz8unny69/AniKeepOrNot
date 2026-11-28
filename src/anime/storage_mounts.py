"""Storage folders that are mounts must be mounted before AKON reads or writes there.

Owner, 2026-11-27: the staging folder is a network share. When its mount is missing,
/mnt/media/Anime-Trial is a plain empty folder on the share above it. Writing there
looks like it works, and the files disappear under the mount when it comes back.

So each folder AKON uses (downloads, staging, the library) is looked up in
/proc/self/mountinfo when it is used. The first time a folder is a mount point, or
lies on a network filesystem, its mount point and source are remembered, in
storage-mounts.json beside the database. From then on the folder must resolve to
that mount again; otherwise it is not mounted, and the work waits for it
(StorageNotMounted is an outage, never a program error). A local folder that was
never a mount stays as simple as before. Nobody is asked; AKON learns it.

- A deeper mount is learned (the share above was seen first, then the folder's own
  mount appeared). A shallower one is the failure this guards against.
- The mount is known by its path and source, never its device number: a remount
  gives the share a new one.
- A folder mounted onto itself (a bind: systemd's ReadWritePaths under ProtectSystem=strict
  does that) is not a share: it has the device of the mount it sits in, so it is passed
  over. Both devices are read at the same moment; none is saved. Production, 2026-11-28:
  /mnt/media/Torrents/anime, a plain folder of the media share, was learned as a mount
  from the worker's ReadWritePaths bind; once the unit stopped binding it, the folder
  'was not mounted' and downloads waited. Facts learned before binds were passed over
  (no 'binds_ignored') are learned again from what is mounted now.
- The facts are keyed by the folder's path: changing the folder setting starts over.
  To make a learned mount local on purpose, delete its entry from the file.

Calls touch the filesystem: on a hard NFS mount they block while the share is away,
so they must run in a worker thread (async_io.blocking), never on the event loop.
"""
import json
import os
from pathlib import Path
import time

FACTS = "storage-mounts.json"
NETWORK = frozenset({"nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "ceph", "glusterfs", "fuse.glusterfs",
                     "fuse.sshfs", "fuse.rclone", "davfs", "afs", "lustre", "beegfs"})
ROOTS = ("storage.download_root", "storage.user_evaluation_root", "storage.keep_root")


class StorageNotMounted(RuntimeError):
    """A folder that was a mount is not mounted now: an outage, so the work waits."""
    reason_code = "STORAGE_UNAVAILABLE"

    def __init__(self, root, expected, found, facts_file=None):
        self.root, self.expected, self.found = root, expected, found
        undo = (f" If it is no longer meant to be its own mount, remove its entry from {facts_file}."
                if facts_file else "")
        super().__init__(f"{root} is not mounted: it was {expected['source']} on {expected['mount_point']}, "
                         f"now it is on {found[0]} ({found[1]}); waiting for the mount.{undo}")


def _unescape(field):
    """mountinfo writes space, tab, newline and backslash as octal escapes."""
    return field.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def _entries(text=None):
    """[(mount id, parent id, device, mount_point, source, fstype)] from /proc/self/mountinfo."""
    if text is None:
        text = Path("/proc/self/mountinfo").read_text()
    found = []
    for line in text.splitlines():
        left, _, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if len(fields) >= 5 and len(tail) >= 2:
            found.append((fields[0], fields[1], fields[2], _unescape(fields[4]), _unescape(tail[1]), tail[0]))
    return found


def mounts(text=None):
    """[(mount_point, source, fstype)] from /proc/self/mountinfo, folders mounted onto themselves left out.

    A mount with the device of the mount it sits in is a folder of that filesystem
    bound in place, not a share of its own.
    """
    entries = _entries(text)
    device = {mount_id: dev for mount_id, _, dev, *_ in entries}
    return [(mount_point, source, fstype) for _, parent, dev, mount_point, source, fstype in entries
            if device.get(parent) != dev]


def _within(path, mount_point):
    return mount_point == "/" or path == mount_point or path.startswith(mount_point.rstrip("/") + "/")


def mount_of(root, text=None):
    """(mount_point, source, fstype) of the filesystem root lies on.

    root is opened first: NFSv4 mounts a share below an export only when a path goes
    into it, and unmounts it again when it is idle. A stat of the folder does not go
    into it (production, 2026-11-27: /mnt/media/Anime was its own share, yet missing
    from mountinfo), so the folder is opened, which mounts its share if it has one.
    """
    os.close(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
    real = os.path.realpath(root)
    best = None
    for mount_point, source, fstype in mounts(text):     # The last mount on a path is the one in use.
        if _within(real, mount_point) and (best is None or len(mount_point) >= len(best[0])):
            best = (mount_point, source, fstype)
    return best


def _facts_file(database_path):
    return Path(database_path).parent / FACTS


def learned(database_path):
    try:
        return json.loads(_facts_file(database_path).read_text())
    except FileNotFoundError:
        return {}


def _save(database_path, facts):
    path = _facts_file(database_path)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(facts, indent=2, sort_keys=True))
    os.replace(partial, path)


def check(database_path, root, *, now_ms=None, text=None):
    """Raise StorageNotMounted if root was a mount and is not mounted now; learn it otherwise.

    Returns 'local', 'mounted', 'learned' (first seen as a mount, or a deeper one) or
    'missing' (no such folder, never a mount: nothing to guard; the code using it decides).
    """
    facts = learned(database_path)
    known = facts.get(root)
    relearn = known is not None and not known.get("binds_ignored")
    if relearn:                                          # Learned before binds were passed over: look again.
        del facts[root]
        known = None
    try:
        mount_point, source, fstype = mount_of(root, text)
    except FileNotFoundError:
        if known is None:
            return "missing"
        raise StorageNotMounted(root, known, ("(the folder is missing)", ""), _facts_file(database_path)) from None
    if known is not None:
        if (mount_point, source) == (known["mount_point"], known["source"]):
            return "mounted"
        deeper = mount_point != known["mount_point"] and _within(mount_point, known["mount_point"])
        if not deeper:
            raise StorageNotMounted(root, known, (mount_point, source), _facts_file(database_path))
    elif not (mount_point == os.path.realpath(root) or fstype in NETWORK):
        if relearn:
            _save(database_path, facts)
        return "local"
    facts[root] = {"mount_point": mount_point, "source": source, "fstype": fstype, "binds_ignored": True,
                   "learned_at_ms": now_ms if now_ms is not None else time.time_ns() // 1_000_000}
    _save(database_path, facts)
    return "learned"


def uses_storage(*keys):
    """Decorate an async step that reads or writes these storage folders (setting names).

    Before the step runs, each folder's mount is checked in a worker thread; a missing
    mount raises StorageNotMounted, an outage, so the series waits and nothing is touched.
    The one place the check is wired: a step says what it uses, the wrapper guards it.
    """
    import functools

    def wrap(step):
        @functools.wraps(step)
        async def guarded(*args, **kwargs):
            from src.anime.async_io import blocking
            context = kwargs.get("context", args[-1] if args else None)
            await blocking(require, context.configuration, *keys)
            return await step(*args, **kwargs)
        guarded.storage = keys
        return guarded
    return wrap


DOWNLOADS, STAGING, LIBRARY = ROOTS


def require(configuration, *keys):
    """check() each configured folder in keys (setting names, e.g. 'storage.user_evaluation_root')."""
    database = configuration.value("storage.database_path")
    for key in keys:
        root = configuration.value(key)
        if root:
            check(database, root)
