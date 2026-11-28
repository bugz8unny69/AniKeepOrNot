"""Display-only anime title selection shared by the API, the dashboard and the staged NFOs.

Owner, 2026-11-27: with English chosen, Intake showed AniDB's main title (usually romaji)
for most series, because only anime_title_metadata was read, and it has an English title
for 112 of 153 series on production. AniDB's title list (already cached for identification)
has the official English title of nearly every entry (141 of 153; with the metadata, 149,
and the other 4 have English main titles). So a title is looked up in the metadata first,
then in that list. Offline: only cached data is read, stale or not.

A TVDB series is named after the AniDB entry Anime-Lists maps to its season 1, so every
season of it shares one name (and one staging folder: Jellyfin shows one series).
"""
import hashlib

_PARSED = {}          # provider -> (digest of the payload, parsed value)


def preferred_title(canonical, english, romaji, language):
    if language == 'english':
        return english or canonical
    if language == 'japanese':
        return romaji or canonical
    raise ValueError('Invalid anime title language')


def _database_path(db):
    row = db.execute('PRAGMA database_list').fetchone()
    return row[2] if row and row[2] else None          # None: an in-memory database (tests).


def _cached(database_path, provider, key, parse):
    """A cached provider payload parsed once per version of it; None when not cached."""
    if not database_path:
        return None
    from src.anime.storage.provider_cache import ProviderStore
    payload = ProviderStore(database_path).public_get_stale(provider, key)
    if not payload:
        return None
    digest = hashlib.blake2b(payload, digest_size=16).digest()
    held = _PARSED.get(provider)
    if held is None or held[0] != digest:
        held = (digest, parse(payload))
        _PARSED[provider] = held
    return held[1]


def _title_list(payload):
    """{aid: (official English title, romaji title)} from AniDB's title list."""
    from src.anime.providers.parsing import xml
    lang = '{http://www.w3.org/XML/1998/namespace}lang'
    found = {}
    for node in xml(payload, 'animetitles').findall('anime'):
        aid = node.get('aid')
        if not aid or not aid.isdigit():
            continue
        english = romaji = None
        for item in node.findall('title'):
            text = (item.text or '').strip()
            if not text:
                continue
            if item.get(lang) == 'en' and item.get('type') == 'official' and english is None:
                english = text
            elif item.get(lang) == 'x-jat' and item.get('type') in ('main', 'official') and romaji is None:
                romaji = text
        if english or romaji:
            found[int(aid)] = (english, romaji)
    return found


def _mapping(payload):
    from src.anime.providers.parsing import xml
    return xml(payload, 'anime-list', limit=16777216)


def names(db, aid):
    """(english, romaji) for an AniDB ID: the metadata, then AniDB's cached title list."""
    row = db.execute('SELECT english_title,romaji_title FROM anime_title_metadata WHERE anidb_id=?',
                     (aid,)).fetchone()
    english, romaji = (row[0], row[1]) if row else (None, None)
    if not (english and romaji):
        listed = (_cached(_database_path(db), 'anidb_titles', 'titles', _title_list) or {}).get(int(aid))
        if listed:
            english, romaji = english or listed[0], romaji or listed[1]
    return english, romaji


def title_for(db, aid, canonical, language):
    """Trust a verified AniDB ID; never infer a title from a TVDB series ID."""
    english, romaji = names(db, aid)
    return preferred_title(canonical, english, romaji, language)


def first_entry(db, tvdb_series_id):
    """The AniDB ID Anime-Lists maps to the TVDB series' first episode (season 1), or None."""
    root = _cached(_database_path(db), 'anime_lists', 'mapping', _mapping)
    if root is None:
        return None
    rows = []
    for node in root.findall('anime'):
        if node.get('tvdbid') != str(tvdb_series_id) or node.get('defaulttvdbseason') != '1':
            continue
        aid, offset = node.get('anidbid') or '', node.get('episodeoffset') or '0'
        if aid.isdigit() and offset.lstrip('-').isdigit():
            rows.append((int(offset), int(aid)))
    return min(rows)[1] if rows else None


def series_title(db, tvdb_series_id, aid, canonical, language):
    """The name of a whole TVDB series: its season-1 AniDB entry's title, else this entry's."""
    first = first_entry(db, tvdb_series_id)
    if first is not None and first != aid:
        has_series = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='series'").fetchone()
        row = db.execute('SELECT title FROM series WHERE anidb_id=?', (first,)).fetchone() if has_series else None
        english, romaji = names(db, first)
        base = row[0] if row else romaji
        named = preferred_title(base, english, romaji, language)
        if named:
            return named
    return title_for(db, aid, canonical, language)
