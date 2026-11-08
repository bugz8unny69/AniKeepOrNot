"""Pure no-side-effect planning for verified season/episode torrent inventories.

A plan is NOT permission to download, reprioritize, stage or Keep a torrent.
Provider evidence, durable ownership and per-file proof are separate gates.
"""
from dataclasses import dataclass
from pathlib import PurePosixPath
import re

from src.anime.acquisition.metadata import BatchInventory, ObservedBatchInventory, safe_path

VIDEO = frozenset(('.mkv', '.mp4', '.m4v', '.avi'))
_SPECIAL = re.compile(r'(?<![A-Za-z0-9])(?:OVA|OAV|ONA|SPECIALS?|NCOP|NCED|PV|TRAILER|RECAP|EXTRAS?|MOVIE)(?![A-Za-z0-9])', re.I)
_SE = re.compile(r'(?<![A-Za-z0-9])S(?P<season>\d{1,2})[ ._-]*E(?P<ep>\d{1,4})(?![A-Za-z0-9])', re.I)
_X = re.compile(r'(?<![A-Za-z0-9])(?P<season>\d{1,2})x(?P<ep>\d{1,4})(?!\d)', re.I)
_SEASON = re.compile(r'(?<![A-Za-z0-9])S(?P<season>\d{1,2})(?![A-Za-z0-9])|\bSeason[ ._-]*S?(?P<word>\d{1,2})\b', re.I)
_SEASON_DIR = re.compile(r'(?:Season[ ._-]*|S)(\d{1,2})', re.I)
_ORDINARY = (re.compile(r'(?<![A-Za-z0-9])(?:Episode|EP|E)[ ._-]*(?P<ep>\d{1,4})(?!\d)', re.I),
             re.compile(r'\s-\s(?P<ep>\d{1,4})(?:v\d+)?(?!\d)', re.I),
             re.compile(r'[\[(](?P<ep>\d{1,4})[\])]', re.I),
             re.compile(r'第\s*(?P<ep>\d{1,4})\s*話'))
_RANGE = re.compile(r'\d{1,4}\s*(?:-|~|\+|&|,|to)\s*(?:E(?:P)?\s*)?\d{1,4}(?!\d)', re.I)
_REPEAT = re.compile(r'(?<![A-Za-z])E\d{1,4}\s*E\d{1,4}(?!\d)', re.I)


class BatchPlanningError(ValueError):
    """Unverified or inconsistent episode-to-file correspondence."""


@dataclass(frozen=True)
class BatchSelection:
    season: int
    start_episode: int
    episode_indices: tuple[tuple[int, int], ...]
    probe_index: int
    remaining_indices: tuple[int, ...]
    skipped_indices: tuple[int, ...]
    extra_indices: tuple[int, ...]


def _episode(path, season):
    """Return a release episode number, None for a labeled extra, else fail."""
    safe_path(path)
    relative = PurePosixPath(path)
    stem = relative.stem
    dirs = relative.parts[:-1]
    folder_seasons = [int(m.group(1)) for d in dirs if (m := _SEASON_DIR.fullmatch(d))]
    seasons = {int(m.group('season') or m.group('word')) for m in _SEASON.finditer(stem)}
    seasons.update(folder_seasons)
    special = bool(_SPECIAL.search(stem) or any(_SPECIAL.fullmatch(d) for d in dirs) or 0 in seasons)
    if special:
        # A regular-episode declaration and a special label in one video are
        # contradictory; skip only files with unambiguous extra identity.
        if any(int(m.group('season')) > 0 for m in _SE.finditer(stem)):
            raise BatchPlanningError('Conflicting regular episode and extra labels')
        if not seasons or seasons == {0} or (seasons == {season} and not _SE.search(stem)):
            return None
        raise BatchPlanningError('Conflicting season in extra video')
    if len(seasons) > 1 or (seasons and season not in seasons):
        raise BatchPlanningError('Conflicting season in video file')
    if _RANGE.search(stem) or _REPEAT.search(stem):
        raise BatchPlanningError('Multi-episode video cannot be mapped one-to-one')
    explicit = [(int(m.group('season')), int(m.group('ep'))) for m in _SE.finditer(stem)]
    explicit += [(int(m.group('season')), int(m.group('ep'))) for m in _X.finditer(stem)]
    if len(explicit) > 1:
        raise BatchPlanningError('Ambiguous explicit episode declarations')
    if explicit:
        file_season, number = explicit[0]
        if file_season != season:
            raise BatchPlanningError('Episode belongs to a different season')
        residue = _SE.sub(' ', _X.sub(' ', stem))
        if any(p.search(residue) for p in _ORDINARY):
            raise BatchPlanningError('Conflicting episode declarations')
    else:
        candidates = []
        for pattern in _ORDINARY:
            for match in pattern.finditer(stem):
                value = int(match.group('ep'))
                if 1900 <= value <= 2100:
                    raise BatchPlanningError('Ambiguous year/episode number')
                candidates.append(value)
        if not candidates and folder_seasons and stem.isdecimal():
            candidates = [int(stem)]
        if not candidates or len(set(candidates)) != 1:
            raise BatchPlanningError('Episode number missing or ambiguous')
        number = candidates[0]
    if not 1 <= number <= 9999:
        raise BatchPlanningError('Invalid regular episode')
    return number


def file_episode_number(path, season):
    """Expose the strict filename parser for independent provider validation."""
    return _episode(path, season)


def plan_verified_batch(inventory, *, season, episode_map, start_episode=1):
    """Prepare a no-side-effect, E01-first plan from an authoritative episode map.

    episode_map maps filename episode numbers to unique canonical TVDB episode
    numbers; its trusted provider provenance MUST be checked upstream.
    """
    if type(inventory) not in (BatchInventory,ObservedBatchInventory):
        raise ValueError('Hash-verified batch inventory required')
    if type(season) is not int or not 1 <= season <= 100:
        raise ValueError('Confirmed positive season required')
    if type(start_episode) is not int or not 1 <= start_episode <= 9994:
        raise ValueError('Verified trial start required')
    if (type(episode_map) is not dict or not episode_map
            or any(type(a) is not int or type(b) is not int or not 1 <= a <= 9999 or not 1 <= b <= 9999
                   for a, b in episode_map.items())
            or len(set(episode_map.values())) != len(episode_map)):
        raise ValueError('Unique, verified episode mapping required')
    assigned, extra, indices = {}, [], set()
    for file in inventory.files:
        indices.add(file.index)
        if PurePosixPath(file.path).suffix.lower() not in VIDEO:
            continue
        number = _episode(file.path, season)
        if number is None:
            extra.append(file.index)
            continue
        canonical = episode_map.get(number)
        if canonical is None:
            raise BatchPlanningError('Video episode lacks verified provider mapping')
        if canonical in assigned:
            raise BatchPlanningError('Duplicate canonical episode in torrent')
        assigned[canonical] = file.index
    if start_episode not in assigned:
        raise BatchPlanningError('First trial episode missing from verified manifest')
    wanted = tuple(sorted((ep, index) for ep, index in assigned.items()
                          if start_episode <= ep < start_episode + 6))
    probe = assigned[start_episode]
    all_selected = {i for _, i in wanted}
    return BatchSelection(season, start_episode, wanted, probe,
                          tuple(i for ep, i in wanted if ep != start_episode),
                          tuple(sorted(indices - all_selected)), tuple(sorted(extra)))
