"""Retitle what is already staged: folders, file names and NFOs in the chosen language.

Owner, 2026-11-27: without Jellyfin or AKON the owner cannot tell which series a folder is;
staged series showed in romaji. New staging already names everything in the chosen
language, one folder per TVDB series (staging.specification). This brings the staged
episodes in line, in the cutover and with `adm retitle`:

- each TVDB series gets one folder, '<series name> [tvdbid-N]' (anime_titles.series_title):
  its seasons from separate AniDB entries come together, as Jellyfin expects;
- each staged video is renamed '<series name> - SxxEyy.<ext>' (a rename on the share: its
  content and content_id do not change), and its NFOs are written again by the same
  code staging uses, with the new title;
- the series rows follow: each plan's entries (paths, NFO size, hash and text) and each
  staged episode's file, so Keep and Reject still know the files as AKON's.

Only AKON's files move. A destination that holds a file AKON did not plan stops that
series (reported, nothing moved for it). Files that are not AKON's stay where they are,
so their folder stays too. plan() reads; apply() moves and writes, keeping an undo list;
undo() puts everything back; finish() removes the replaced NFOs and the empty folders.
"""
import os
from types import SimpleNamespace

BACKUP = ".retitle-old"


def _items(series):
    """(anidb_id, title, torrents, episodes) from series rows or the cutover's dicts."""
    for item in series:
        if isinstance(item, dict):
            yield item["anidb_id"], item.get("title"), item["torrents"], item["episodes"]
        else:
            yield item.anidb_id, item.title, item.torrents, item.episodes


def plan(db, series, language):
    """What retitling would do: {"groups": [...], "skipped": [...]}; nothing is touched."""
    from src.anime.anime_titles import series_title
    from src.anime.staging import specification
    groups = {}
    for aid, title, torrents, episodes in _items(series):
        for torrent in torrents:
            tvdb = ((torrent.get("identity") or {}).get("anime") or {}).get("tvdb_series_id")
            files = {f.get("episode"): f for f in torrent.get("files", [])}
            for code, old in ((torrent.get("staging") or {}).get("plans") or {}).items():
                if not tvdb or not old.get("root") or (episodes.get(code) or {}).get("state") != "staged":
                    continue
                group = groups.setdefault((old["root"], int(tvdb)), {"root": old["root"], "tvdb": int(tvdb),
                                                                     "first": aid, "title": title, "episodes": []})
                group["episodes"].append({"anidb_id": aid, "title": title, "code": code, "plan": old,
                                          "file": files.get(code) or {}})
    result, skipped = [], []
    for (root, tvdb), group in sorted(groups.items()):
        name = series_title(db, tvdb, group["first"], group["title"], language)
        folder = f"{_slug(name)} [tvdbid-{tvdb}]"
        moves, problem = [], None
        for ep in group["episodes"]:
            old, f = ep["plan"], ep["file"]
            media = next(e for e in old["entries"] if e["role"] == "media")
            season, number = int(ep["code"][1:3]), int(ep["code"][4:])
            if not f.get("anidb_episode_id"):
                problem = f"{ep['code']} has no AniDB episode ID to write in its NFO"
                break
            identity = (ep["anidb_id"], f["anidb_episode_id"], tvdb, season, number,
                        f.get("tvdb_episode_id"), ep["title"] or name)
            evidence = {"path": media["path"], "sha256": media["sha256"],
                        "file": [0, 0, media["size"], 0, 0], "content": media.get("content")}
            new = specification(SimpleNamespace(options=SimpleNamespace(download_root=old.get("source_root", ""))),
                                evidence, identity, root, old.get("revision") or 1, display_title=name,
                                series_name=name, folder=folder)
            moves.append({"code": ep["code"], "plan": old, "entries": new["entries"],
                          "old_paths": [e["path"] for e in old["entries"]],
                          "old_media": next(e["path"] for e in old["entries"] if e["role"] == "media")})
        if problem is None:
            # One tvshow.nfo per series: every season's plan carries the first one's.
            shared = next(e for e in moves[0]["entries"] if e["role"] == "series")
            for m in moves:
                m["entries"] = [dict(shared) if e["role"] == "series" else e for e in m["entries"]]
            problem = _collision(root, moves)
        if problem:
            skipped.append({"tvdb": tvdb, "name": name, "why": problem})
            continue
        if all(m["entries"][0]["path"] == m["old_media"] and _same_nfos(root, m) for m in moves):
            continue                                       # Already named this way.
        result.append({"root": root, "tvdb": tvdb, "name": name, "folder": folder, "moves": moves})
    return {"groups": result, "skipped": skipped}


def _slug(name):
    from src.anime.staging import slug
    return slug(name)


def _same_nfos(root, move):
    for entry in move["entries"]:
        if entry["role"] == "media":
            continue
        try:
            with open(os.path.join(root, entry["path"]), encoding="utf-8") as current:
                if current.read() != entry["text"]:
                    return False
        except FileNotFoundError:
            return False
    return True


def _collision(root, moves):
    """A destination that holds a file AKON did not plan there: why, or None."""
    ours = {path for m in moves for path in m["old_paths"]}
    for m in moves:
        for entry in m["entries"]:
            if entry["role"] == "series":
                continue                                   # tvshow.nfo is rewritten: it is the series'.
            target = os.path.join(root, entry["path"])
            if os.path.lexists(target) and entry["path"] not in ours:
                return f"{entry['path']} is already there and is not a file AKON staged"
    return None


def apply(retitle):
    """Rename the videos, write the NFOs; returns the undo list (undo() reverses it)."""
    done = []
    try:
        for group in retitle["groups"]:
            root = group["root"]
            series_written = False
            for move in group["moves"]:
                old = {e["role"]: e for e in move["plan"]["entries"]}   # Before rewrite(): the old paths.
                for entry in move["entries"]:
                    target = os.path.join(root, entry["path"])
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    if entry["role"] == "media":
                        source = os.path.join(root, old["media"]["path"])
                        if source != target and os.path.exists(source):
                            os.rename(source, target)
                            done.append(("rename", target, source))
                        continue
                    previous = os.path.join(root, old[entry["role"]]["path"]) if entry["role"] in old else None
                    if previous and previous != target and os.path.exists(previous):
                        os.rename(previous, previous + BACKUP)          # The old NFO: removed by finish().
                        done.append(("rename", previous + BACKUP, previous))
                    if entry["role"] == "series" and series_written:
                        continue                                       # One tvshow.nfo per series.
                    series_written = series_written or entry["role"] == "series"
                    if os.path.exists(target):
                        os.rename(target, target + BACKUP)
                        done.append(("rename", target + BACKUP, target))
                    with open(target + ".retitle-new", "w", encoding="utf-8") as written:
                        written.write(entry["text"])
                    os.replace(target + ".retitle-new", target)
                    done.append(("remove", target))
    except BaseException:
        undo(done)
        raise
    return done


def undo(done):
    for step in reversed(done):
        if step[0] == "remove":
            try:
                os.unlink(step[1])
            except FileNotFoundError:
                pass
        else:
            os.rename(step[1], step[2])


def finish(retitle):
    """After the rows are saved: drop the replaced NFOs, then every folder left empty."""
    folders = set()
    for group in retitle["groups"]:
        root = group["root"]
        for move in group["moves"]:
            for relative in move["old_paths"]:
                backup = os.path.join(root, relative) + BACKUP
                if os.path.exists(backup):
                    os.unlink(backup)
                parts = relative.split("/")
                folders.update(os.path.join(root, *parts[:n]) for n in range(1, len(parts)))
            for entry in move["entries"]:
                backup = os.path.join(root, entry["path"]) + BACKUP
                if os.path.exists(backup):
                    os.unlink(backup)
    for folder in sorted(folders, key=lambda f: -f.count(os.sep)):
        try:
            os.rmdir(folder)
        except OSError:
            pass                                           # Not empty, or gone.


def rewrite(series, retitle):
    """Point the rows at the new names: plan entries (keeping each plan's own fields) and files."""
    new = {(m["plan"]["root"], id(m["plan"])): m for g in retitle["groups"] for m in g["moves"]}
    for _, _, torrents, episodes in _items(series):
        for torrent in torrents:
            for code, old in ((torrent.get("staging") or {}).get("plans") or {}).items():
                move = new.get((old.get("root"), id(old)))
                if move is None:
                    continue
                media = next(e for e in old["entries"] if e["role"] == "media")
                entries = []
                for entry in move["entries"]:
                    entry = dict(entry)
                    if entry["role"] == "media":           # Same video: its recorded identity stays.
                        entry.update({k: media[k] for k in ("sha256", "size", "content") if k in media})
                    entries.append(entry)
                old["entries"] = entries
                if code in episodes:
                    episodes[code]["file"] = next(e["path"] for e in entries if e["role"] == "media")
