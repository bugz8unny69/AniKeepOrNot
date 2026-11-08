"""Advisory release scope hints; never evidence for acquiring an episode.

The webhook title is untrusted. Only canonical provider mappings and a
hash-verified per-file torrent inventory may authorize a later selection.
"""
from dataclasses import dataclass
import re

_SEASON_EP = re.compile(r'(?<![A-Za-z0-9])S(?P<season>\d{1,2})[ ._-]*E(?P<episode>\d{1,4})(?![A-Za-z0-9])', re.I)
_SEASON = re.compile(r'(?<![A-Za-z0-9])S(?P<season>\d{1,2})(?![A-Za-z0-9])|\bSeason[ ._-]*S?(?P<word>\d{1,2})\b', re.I)
_PACK = re.compile(r'\b(?:Batch|Complete[ ._-]*Season|Season[ ._-]*Pack|Complete)\b', re.I)
_SPECIAL = re.compile(r'\b(?:OVA|OAV|ONA|Specials?|NCOP|NCED|PV|Trailer|Recap|Movie|Extras?)\b', re.I)
_RANGE = re.compile(r'(?<!\d)\d{1,4}\s*(?:-|~|&|\+|,|to)\s*(?:E(?:P)?\s*)?\d{1,4}(?!\d)', re.I)
# Concatenated episodes (S01E01E02); a dash is a range and handled by _RANGE.
_REPEAT = re.compile(r'(?<![A-Za-z])E\d{1,4}[ ._]*E\d{1,4}(?!\d)', re.I)


@dataclass(frozen=True)
class ReleaseScope:
    kind: str
    season_hint: int | None = None
    episode_hint: int | None = None


def release_scope_hint(release_name):
    """Classify titles for routing/diagnostics only; no episode assertions."""
    unknown = ReleaseScope('unknown')
    if type(release_name) is not str or not 1 <= len(release_name) <= 2048:
        return unknown
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in release_name):
        return unknown
    # One clear episode, as the shared release-group title reader sees it
    # ("Title S2 - 05", "Mob Psycho 100 - 05"), is never a pack or a range.
    from src.nextgen.release_names import marker_seasons, parse_release
    parsed = parse_release(release_name)
    if parsed is not None and parsed.season != 0:
        seasons = ({parsed.season} if parsed.season is not None
                   else marker_seasons(parsed.text, roman=False))
        if len(seasons) <= 1:
            return ReleaseScope('single_episode', next(iter(seasons), None), parsed.episode)
    episodes = list(_SEASON_EP.finditer(release_name))
    seasons = {int(m.group('season') or m.group('word')) for m in _SEASON.finditer(release_name)}
    seasons.update(int(m.group('season')) for m in episodes)
    if len(seasons) > 1 or len(episodes) > 1 or _REPEAT.search(release_name):
        return ReleaseScope('ambiguous')
    season = next(iter(seasons), None)
    special = bool(_SPECIAL.search(release_name))
    episode_range = _RANGE.search(release_name)
    if episode_range:
        first, last = (int(value) for value in re.findall(r'\d+', episode_range.group()))
        # A single-season episode range is a batch, not an invalid single episode.
        # Torrent inventory and providers establish the actual per-file coverage.
        if (season is None or season == 0 or special or first < 1 or last <= first
                or (episodes and int(episodes[0].group('episode')) != first)):
            return ReleaseScope('ambiguous', season)
        return ReleaseScope('season_pack', season, first)
    if season == 0:
        return ReleaseScope('special', 0, int(episodes[0].group('episode')) if episodes else None)
    if episodes:
        number = int(episodes[0].group('episode'))
        if number < 1 or special or _PACK.search(release_name):
            return ReleaseScope('ambiguous', season)
        return ReleaseScope('single_episode', season, number)
    if season is not None or _PACK.search(release_name):
        return ReleaseScope('mixed_pack' if special else 'season_pack', season)
    if special:
        return ReleaseScope('special')
    return unknown
