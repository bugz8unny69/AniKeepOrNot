"""Bounded parsing of provider responses (AniDB XML, TVDB JSON).

Release-name parsing lives in src/anime/release_names.py.
"""
import re
import xml.etree.ElementTree as ET

from src.anime.providers.errors import ProviderInvalidResponse
from src.anime.storage.sqlite import IdentityConflict, IdentityNotReady
from src.anime.release_names import norm


def xml(payload, root_tag, *, limit=67108864):
    try:
        if type(payload) is not bytes or not 1 <= len(payload) <= limit:
            raise ValueError()
        text = payload.decode("utf-8-sig")
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise ValueError()
        root = ET.fromstring(text)
        if root.tag != root_tag: raise ValueError()
        return root
    except (ValueError, UnicodeError, ET.ParseError):
        raise ProviderInvalidResponse() from None


def positive_text(value):
    if type(value) is not str or not re.fullmatch(r"[1-9][0-9]{0,17}",value):
        raise ProviderInvalidResponse()
    return int(value)


def title_matches(payload, aliases, *, threshold_pct=50):
    root = xml(payload, "animetitles")
    wanted_texts = [a.text if hasattr(a, "text") else str(a) for a in aliases if a]
    wanted_norms = {norm(t) for t in wanted_texts if norm(t)}
    if not wanted_norms:
        return {}

    wanted_words = {
        w for t in wanted_texts
        for w in re.findall(r'[a-z0-9]{3,}', norm(t))
    }

    from difflib import SequenceMatcher
    found = {}
    for node in root.findall("anime"):
        aid = positive_text(node.get("aid"))
        if aid in found:
            raise ProviderInvalidResponse()
        titles = tuple((item.text or "").strip() for item in node.findall("title") if (item.text or "").strip())
        if not titles:
            continue

        is_match = any(norm(title) in wanted_norms for title in titles)
        if not is_match and threshold_pct is not None and wanted_words:
            for title in titles:
                nt = norm(title)
                if not nt:
                    continue
                if any(w in nt for w in wanted_words):
                    best_ratio = max(
                        SequenceMatcher(None, nw, nt, autojunk=False).ratio()
                        for nw in wanted_norms
                    )
                    if round(100 * best_ratio) >= threshold_pct:
                        is_match = True
                        break

        if is_match:
            found[aid] = titles
            if len(found) > 1000:
                raise IdentityNotReady()

    return found


def mapping_id(payload, aid):
    root = xml(payload,"anime-list",limit=16777216)
    rows = [node for node in root.findall("anime") if node.get("anidbid")==str(aid)]
    if not rows: raise IdentityNotReady()
    if len(rows)!=1: raise IdentityConflict()
    value = rows[0].get("tvdbid")
    if value in (None,"","0","unknown"): raise IdentityNotReady()
    return positive_text(value)


def season_entry(payload, tid, season, episode=None):
    """AniDB entry that anime-lists records as TVDB season N of series tid.

    A split season has several entries; the one whose episode offset is the
    largest below the episode number covers it. Release verification still
    proves the exact episode mapping before anything is downloaded.
    """
    root = xml(payload,"anime-list",limit=16777216)
    rows = []
    for node in root.findall("anime"):
        if node.get("tvdbid")!=str(tid) or node.get("defaulttvdbseason")!=str(season): continue
        aid, offset = node.get("anidbid") or "", node.get("episodeoffset") or "0"
        if re.fullmatch(r"[1-9][0-9]{0,9}",aid) and re.fullmatch(r"-?[0-9]{1,5}",offset):
            rows.append((int(offset),int(aid)))
    if episode is not None: rows = [row for row in rows if row[0]<episode]
    if not rows: raise IdentityNotReady()
    best = max(offset for offset,_ in rows)
    picked = [aid for offset,aid in rows if offset==best]
    if len(picked)!=1: raise IdentityConflict()
    return picked[0]


def anime_record(payload, aid):
    root = xml(payload,"anime",limit=4194304)
    if root.get("id")!=str(aid): raise ProviderInvalidResponse()
    entries = [item for item in root.findall("titles/title") if (item.text or "").strip()]
    titles = [(item.text or "").strip() for item in entries]
    main = [(item.text or "").strip() for item in entries if item.get("type") == "main"]
    if len(main) != 1 or not titles:
        raise ProviderInvalidResponse()

    def translated(language):
        # AniDB XML annotates titles using xml:lang and type=main/official/syn.
        matches = [item for item in entries
                   if item.get("{http://www.w3.org/XML/1998/namespace}lang",
                               item.get("lang")) == language]
        preferred = next((item for item in matches if item.get("type") == "official"), None)
        return preferred if preferred is not None else (matches[0] if matches else None)

    english = translated("en")
    romaji = translated("x-jat")
    native = translated("ja")
    return {"anidb_id": aid, "title": main[0], "titles": tuple(titles),
            "english_title": english.text.strip() if english is not None else None,
            "romaji_title": romaji.text.strip() if romaji is not None else None,
            "original_title": native.text.strip() if native is not None else None}


def tvdb_record(document, tid):
    if type(document) is not dict or type(document.get("data")) is not dict:
        raise ProviderInvalidResponse()
    record = document["data"]
    if type(record.get("id")) is not int or record["id"]!=tid:
        raise ProviderInvalidResponse()
    title = record.get("name")
    if type(title) is not str or not title.strip(): raise ProviderInvalidResponse()
    names = [title]
    aliases = record.get("aliases") or []
    if type(aliases) is not list: raise ProviderInvalidResponse()
    for item in aliases:
        name = item.get("name") if type(item) is dict else item
        if type(name) is str and name.strip(): names.append(name)
    translations = record.get("translations") or {}
    if type(translations) is not dict: raise ProviderInvalidResponse()
    translated = translations.get("nameTranslations") or []
    if type(translated) is not list: raise ProviderInvalidResponse()
    for item in translated:
        if type(item) is dict and type(item.get("name")) is str and item["name"].strip():
            names.append(item["name"])
    genres = record.get("genres") or []
    if type(genres) is not list: raise ProviderInvalidResponse()
    terms = {str(item.get("name","")).strip().casefold() for item in genres if type(item) is dict}
    animation = any(term in {"animation","anime"} for term in terms)
    live = any(term in {"live action","live-action"} for term in terms)
    ids = set()
    remote = record.get("remoteIds") or []
    if type(remote) is not list: raise ProviderInvalidResponse()
    for item in remote:
        if type(item) is not dict: continue
        source = item.get("sourceName") or item.get("source")
        if type(source) is dict: source = source.get("name")
        if type(source) is str and source.strip().casefold()=="anidb":
            value = item.get("id")
            if type(value) is int: value = str(value)
            ids.add(positive_text(value))
    if len(names)>128 or any(len(name)>512 for name in names) or len(ids)>128:
        raise ProviderInvalidResponse()
    return {"tvdb_series_id":tid,"titles":tuple(names),"animation":animation,"live":live,"anidb_ids":frozenset(ids)}


def episode_catalog(payload, aid):
    """Adapt legacy exact regular AniDB epno/eid checks, with no TVDB guessing."""
    root = xml(payload,"anime",limit=4194304)
    if root.get("id")!=str(aid): raise ProviderInvalidResponse()
    container = root.find("episodes")
    if container is None: raise ProviderInvalidResponse()
    episodes,ids = {},set()
    for node in container.findall("episode"):
        number = node.find("epno")
        if number is None or number.get("type")!="1": continue
        epno = positive_text((number.text or "").strip())
        eid = positive_text(node.get("id"))
        if epno in episodes or eid in ids: raise ProviderInvalidResponse()
        episodes[epno] = eid
        ids.add(eid)
        if len(episodes)>10000: raise ProviderInvalidResponse()
    return episodes


def episode_airdates(payload, aid):
    """Regular AniDB episode air dates (ISO date text, or None when unknown)."""
    root = xml(payload,"anime",limit=4194304)
    if root.get("id")!=str(aid): raise ProviderInvalidResponse()
    container = root.find("episodes")
    if container is None: raise ProviderInvalidResponse()
    dates = {}
    for node in container.findall("episode"):
        number = node.find("epno")
        if number is None or number.get("type")!="1": continue
        epno = positive_text((number.text or "").strip())
        text = (node.findtext("airdate") or "").strip()
        dates[epno] = text if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}",text) else None
    return dates


def tvdb_episode_page(document, tid):
    if type(document) is not dict: raise ProviderInvalidResponse()
    data=document.get("data")
    items=data.get("episodes") if type(data) is dict else data
    if type(items) is not list or len(items)>1000: raise ProviderInvalidResponse()
    links=document.get("links",{})
    if type(links) is not dict: raise ProviderInvalidResponse()
    next_page=links.get("next")
    if next_page is not None and (type(next_page) is not str or len(next_page)>2048):
        raise ProviderInvalidResponse()
    records=[]
    seen=set()
    for item in items:
        if type(item) is not dict or type(item.get("id")) is not int or item["id"]<=0:
            raise ProviderInvalidResponse()
        if type(item.get("seriesId")) is not int or item["seriesId"]!=tid: raise ProviderInvalidResponse()
        if item["id"] in seen: raise ProviderInvalidResponse()
        seen.add(item["id"])
        season,number=item.get("seasonNumber"),item.get("number")
        if season is None or number is None or number==0: continue  # Unassigned metadata is not evidence.
        if type(season) is not int or season<0 or type(number) is not int or number<1:
            raise ProviderInvalidResponse()
        records.append((item["id"],season,number))
    return tuple(records),bool(next_page)
