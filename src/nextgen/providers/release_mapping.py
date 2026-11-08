"""Conservative declared-episode parsing and explicit Anime-Lists mapping.

Adapted from legacy release_identity/anime_identity_mapping_v4. No trial-window,
season-one policy, filename authority, unsupported split mapping or inferred
episode placement beyond Anime-Lists' documented zero-offset defaults.
"""
import re

from src.nextgen.providers.parsing import xml
from src.nextgen.release_contracts import ReleaseNotReady, ReleaseConflict


def declared_episode(title):
    """('tvdb', season, number) for SxxEyy, ('anidb', None, number) otherwise.

    Shares the release-group title reader with backfill search
    (acquisition.release_titles), so intake and backfill read titles alike.
    """
    from src.nextgen.release_names import EXPLICIT, marker_seasons, read_release
    parsed, reason = read_release(title)
    if parsed is None: raise ReleaseNotReady(reason)
    if parsed.season is not None:
        # An added "Season N" or "S3" anywhere (e.g. "| Sasaki to Pii-chan Season 2") must agree.
        if marker_seasons(EXPLICIT.sub(' ', parsed.text)) - {parsed.season}:
            raise ReleaseNotReady("RELEASE_TITLE_UNCLEAR")
        return "tvdb", parsed.season, parsed.episode
    if len(marker_seasons(parsed.text)) > 1: raise ReleaseNotReady("RELEASE_TITLE_UNCLEAR")
    return "anidb", None, parsed.episode


def forward_mappings(payload, aid, tid, catalog):
    """Resolve regular AniDB numbers through supported explicit mapping rules.

    Anime-Lists defines omitted/blank offsets as zero when episode numbers
    align. A mapped TVDB season and authoritative episode catalog are still
    required. Unknown/split/absolute rules fail closed.
    """
    root=xml(payload,"anime-list",limit=16777216)
    rows=[node for node in root.findall("anime") if node.get("anidbid")==str(aid)]
    if len(rows)!=1: raise ReleaseNotReady("RELEASE_MAPPING_MISSING")
    node=rows[0]
    if node.get("tvdbid")!=str(tid): raise ReleaseConflict()
    rules=[]
    for item in node.findall("mapping-list/mapping"):
        if item.get("anidbseason")!="1": continue
        # Anime-Lists also contains TMDB-only mappings; those are not TVDB rules.
        season=item.get("tvdbseason")
        if season is None: continue
        if not re.fullmatch(r"[0-9]{1,3}",season): raise ReleaseNotReady("RELEASE_MAPPING_UNSUPPORTED")
        target_season=int(season)
        text=(item.text or "").strip()
        for token in text.strip(";").split(";") if text else ():
            if not token: continue
            match=re.fullmatch(r"([1-9][0-9]{0,3})-([1-9][0-9]{0,3})",token.strip())
            if not match: raise ReleaseNotReady("RELEASE_MAPPING_UNSUPPORTED")
            source,target=map(int,match.groups())
            rules.append((2,source,source,target_season,target-source))
        start,end=item.get("start"),item.get("end")
        if start is not None or end is not None:
            offset=item.get("offset") or "0"
            # An omitted end means the range continues through later AniDB episodes.
            # Resolve only against episode numbers present in the AniDB catalog.
            last=end if end is not None else "9999"
            if (not re.fullmatch(r"[1-9][0-9]{0,3}",start or "") or not re.fullmatch(r"[1-9][0-9]{0,3}",last)
                    or not re.fullmatch(r"-?[0-9]{1,5}",offset or "") or int(start)>int(last)):
                raise ReleaseNotReady("RELEASE_MAPPING_UNSUPPORTED")
            rules.append((1,int(start),int(last),target_season,int(offset)))
    default_season,default_offset=node.get("defaulttvdbseason"),node.get("episodeoffset") or "0"
    default=None
    if re.fullmatch(r"[0-9]{1,3}",default_season or "") and re.fullmatch(r"-?[0-9]{1,5}",default_offset or ""):
        default=(int(default_season),int(default_offset))
    resolved={}
    for number in catalog:
        applicable=[rule for rule in rules if rule[1]<=number<=rule[2]]
        if applicable:
            priority=max(rule[0] for rule in applicable)
            targets={(rule[3],number+rule[4]) for rule in applicable if rule[0]==priority}
            if len(targets)!=1: raise ReleaseConflict()
            target=targets.pop()
        elif default is not None:
            target=(default[0],number+default[1])
        else: continue
        if target[1]>0: resolved[number]=target
    return resolved
