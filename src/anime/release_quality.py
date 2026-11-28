"""Sonarr's quality and release group for a release, from its name (owner, 2026-11-28).

Keep imports AKON's renamed files ('<series> - S01E01.mkv'), which carry no source or
group: Sonarr guessed HDTV-1080p and a group from the title, so any WEB 1080p release
was an upgrade and Sonarr downloaded the episode again. The release name has both.

A release with no source tag (most fansub and encode groups: '[Judas] ... [1080p]') is
taken as WEBRip, what such groups encode from; Sonarr ranks WEBRip and WEB-DL together.
"""
import re

# Sonarr's fixed quality IDs (Quality.cs): (source, resolution) -> (id, name).
QUALITIES = {
    ("web", 480): (8, "WEBDL-480p"), ("webRip", 480): (12, "WEBRip-480p"), ("bluray", 480): (13, "Bluray-480p"),
    ("television", 720): (4, "HDTV-720p"), ("web", 720): (5, "WEBDL-720p"), ("webRip", 720): (14, "WEBRip-720p"),
    ("bluray", 720): (6, "Bluray-720p"),
    ("television", 1080): (9, "HDTV-1080p"), ("web", 1080): (3, "WEBDL-1080p"), ("webRip", 1080): (15, "WEBRip-1080p"),
    ("bluray", 1080): (7, "Bluray-1080p"),
    ("television", 2160): (16, "HDTV-2160p"), ("web", 2160): (18, "WEBDL-2160p"), ("webRip", 2160): (17, "WEBRip-2160p"),
    ("bluray", 2160): (19, "Bluray-2160p"),
}

_RESOLUTION = re.compile(r"(?i)(?<![0-9])(480|576|720|1080|2160)[pi](?![a-z0-9])|(?<![a-z0-9])(4k|uhd)(?![a-z0-9])")
_SOURCES = (
    ("bluray", re.compile(r"(?i)(?<![a-z])(blu-?ray|bd(?:rip|remux)?|bdmv)(?![a-z])")),
    ("webRip", re.compile(r"(?i)(?<![a-z])web-?rip(?![a-z])")),
    ("web", re.compile(r"(?i)(?<![a-z])(web-?dl|web|amzn|nf|cr|dsnp|hidive|adn|bili(?:bili)?|hulu|atvp)(?![a-z])")),
    ("television", re.compile(r"(?i)(?<![a-z])(hdtv|tvrip)(?![a-z])")),
)
_NOT_GROUPS = {"audio", "subs", "sub", "dl", "rip", "dual", "multi"}
_LEADING_GROUP = re.compile(r"^\s*\[([^\[\]]{1,40})\]")
_TRAILING_GROUP = re.compile(r"-([A-Za-z0-9][A-Za-z0-9_]{1,30})(?:\.[A-Za-z0-9]{2,4})?\s*$")


def resolution(name):
    m = _RESOLUTION.search(name or "")
    if not m:
        return None
    if m.group(2):
        return 2160
    return 480 if m.group(1) in ("480", "576") else int(m.group(1))


def source(name):
    for value, pattern in _SOURCES:
        if pattern.search(name or ""):
            return value
    return None


def quality(name, fallback_resolution=None):
    """Sonarr's quality object for the release, or None when its resolution is unknown."""
    height = resolution(name) or fallback_resolution
    found = QUALITIES.get((source(name) or "webRip", height))
    if found is None:
        return None
    return {"quality": {"id": found[0], "name": found[1], "source": source(name) or "webRip", "resolution": height},
            "revision": {"version": 1, "real": 0, "isRepack": False}}


def group(name):
    """The release group: '[Group] ...' or '...-GROUP', else None."""
    m = _LEADING_GROUP.match(name or "")
    if m:
        return m.group(1).strip()
    m = _TRAILING_GROUP.search(name or "")
    if not m or m.group(1).lower() in _NOT_GROUPS:              # '... Dual-Audio', '... WEB-DL'
        return None
    return m.group(1)
