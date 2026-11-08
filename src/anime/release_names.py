"""Read release names: one module for every release-name pattern.

Pure text parsing: no database, no network, no provider types. Everything that
reads a release name (intake identification, recognition, Prowlarr search,
batch discovery) uses this module, so they cannot disagree.

Covers the common styles seen on anime indexers, for example:

    [SubsPlease] Title - 05 (1080p) [ABCD1234].mkv
    [Erai-raws] Title 2nd Season - 05 [1080p CR WEB-DL AVC AAC][MultiSub]
    [Judas] Title - S02E05 [1080p][HEVC x265 10bit]
    Title S02E05 1080p CR WEB-DL AAC2.0 H 264-VARYG
    The.Title.S02E05.1080p.WEB.h264-GROUP
    [Group] Title S2 - 05 / Title Season 2 - 05 / Title II - 05
    [Group] Romaji Title | English Title - 05 (1080p)

Batch/range declarations and non-episode media are rejected by read_release.
A title alone never authorizes acquisition.
"""
from dataclasses import dataclass
import re
import unicodedata


LEADING_TAGS = re.compile(r'^\s*(?:[\[【][^\]】]*[\]】]\s*)+')

EXTENSION = re.compile(r'\.(?:mkv|mp4|avi|webm)$', re.I)

EXPLICIT = re.compile(r'(?<![A-Za-z0-9])S(\d{1,2})\s?E(\d{1,4})(?:v\d{1,2})?(?![0-9A-Za-z])', re.I)

DASHED = re.compile(r'\s[-–—]\s+(?:(?:Episode|Ep\.?|E)\s*)?(\d{1,4})(?:v\d{1,2})?(?=$|[\s\[\(【])', re.I)

WORDED = re.compile(r'(?<![A-Za-z0-9])(?:Episode|Ep\.?|E)\s*(\d{1,4})(?:v\d{1,2})?(?![0-9A-Za-z])', re.I)

SEASON_ONLY = re.compile(r'(?<![A-Za-z0-9])S\d{1,2}(?![0-9A-Za-z])|Season\s*\d', re.I)

# Bare numbers only when zero-padded ("Title 05 [1080p]"); "Mob Psycho 100" is a name.
TRAILING = re.compile(r'\s(\d{2})(?:v\d{1,2})?\s*(?=[\[\(【]|$)')

MULTI = re.compile(r'^\s*(?:v\d+\s*)?(?:[-~&+,]|to)\s*(?:S\d+\s?)?(?:E|EP)?\s*\d', re.I)

NOT_ONE_EPISODE = re.compile(
    r'(?<![A-Za-z])(?:batch|complete|ova|oad|ona|movie|specials?|sp\d*|ncop|nced|pv|cm|'
    r'trailer|recap|preview|extras?|bonus|menu)(?![A-Za-z])', re.I)

ORDINAL_WORDS = {'second': 2, 'third': 3, 'fourth': 4, 'fifth': 5, 'sixth': 6}

MARKERS = (
    re.compile(r'(?<![A-Za-z0-9])S(\d{1,2})(?![A-Za-z0-9])', re.I),
    re.compile(r'(?<![A-Za-z])Season\s*(\d{1,2})(?![0-9])', re.I),
    re.compile(r'(?<![A-Za-z0-9])(\d{1,2})(?:st|nd|rd|th)\s+Season(?![A-Za-z])', re.I),
    re.compile(r'(?<![A-Za-z])(second|third|fourth|fifth|sixth)\s+Season(?![A-Za-z])', re.I),
    re.compile(r'\s(II|III|IV|V|VI)\s*$'),
)

ROMAN = {'II': 2, 'III': 3, 'IV': 4, 'V': 5, 'VI': 6}

def season_marker(text, *, roman=True):
    """(text without its season marker, season number or None).

    Roman numerals ("II") are only read when roman=True: in identity lookup
    they are usually part of the AniDB title itself.
    """
    for pattern in (MARKERS if roman else MARKERS[:-1]):
        match = pattern.search(text)
        if match is None: continue
        token = match.group(1)
        number = (int(token) if token.isdigit() else ORDINAL_WORDS.get(token.lower())
                  or ROMAN.get(token))
        if not number: continue
        rest = (text[:match.start()] + ' ' + text[match.end():])
        rest = re.sub(r'\(\s*\)', ' ', rest)
        return re.sub(r'\s+', ' ', rest).strip(' -:,'), number
    return text.strip(), None

@dataclass(frozen=True)
class ParsedRelease:
    titles: tuple[str, ...]          # title alternatives, as written
    season: int | None               # explicit SxxEyy season
    episode: int
    text: str = ''                   # the normalized title that was read

UNCLEAR, UNSUPPORTED = 'RELEASE_TITLE_UNCLEAR', 'RELEASE_TYPE_UNSUPPORTED'

def _type_words(text):
    """Batch/special words in tags and short parentheses; long ones are alternate names."""
    tags = re.findall(r'\[([^\]]*)\]', text)
    tags += [inner for inner in re.findall(r'\(([^()]*)\)', text) if len(inner.split()) <= 3]
    bare = re.sub(r'\([^()]*\)', ' ', text)
    return bool(NOT_ONE_EPISODE.search(' '.join(tags)) or NOT_ONE_EPISODE.search(bare))

def read_release(title):
    """(ParsedRelease, None) for exactly one episode, else (None, reason code)."""
    if type(title) is not str or not 1 <= len(title) <= 2048: return None, UNCLEAR
    if any(ord(c) < 32 or ord(c) == 127 for c in title): return None, UNCLEAR
    text = EXTENSION.sub('', title.strip()).replace('_', ' ')
    if text.count(' ') < 2 and text.count('.') >= 3:
        text = re.sub(r'\.(?!\d)', ' ', text)    # scene style: dots between words
    text = LEADING_TAGS.sub('', text).strip()
    season = None
    match = EXPLICIT.search(text)
    if match:
        season, episode = int(match.group(1)), int(match.group(2))
    else:
        match = DASHED.search(text) or WORDED.search(text) or TRAILING.search(text)
        if match is None:
            return None, UNSUPPORTED if _type_words(text) or SEASON_ONLY.search(text) else UNCLEAR
        episode = int(match.group(1))
    base, tail = text[:match.start()], text[match.end():]
    if MULTI.match(tail) or EXPLICIT.search(tail) or re.match(r'^\s*-?\s*(?:EP|E)\d', tail, re.I):
        return None, UNSUPPORTED                        # ranges and S01E01E02
    if re.match(r'^\.\d{1,2}(?![0-9pP])', tail) or episode < 1: return None, UNCLEAR
    # A named "Special Part 1" may be the official title of a complete
    # TVDB S00 episode. The exact provider mapping decides, not the words.
    # "Special" may be part of a show's literal name: "Should Be Special".
    # Keep standalone qualifiers and tags as release-type evidence.
    if season != 0 and re.search(r'(?i)(?:^|[-–—]\s+)specials?\s*$', base):
        return None, UNSUPPORTED
    if season != 0 and re.search(r'(?i)\[(?:specials?)\]|\((?:specials?)\)', base):
        return None, UNSUPPORTED
    kind_base = re.sub(r'(?i)\bspecials?\b', ' ', base)
    kind_tail = tail
    if season == 0:
        # TVDB S00 is explicitly a special; packs and movies still fail.
        kind_base = re.sub(r'(?i)\b(?:ova|oad|ona)\b', ' ', kind_base)
        kind_tail = re.sub(r'(?i)\b(?:specials?|ova|oad|ona)\b', ' ', kind_tail)
    if _type_words(kind_base + ' ' + kind_tail): return None, UNSUPPORTED
    base = re.sub(r'[\[【][^\]】]*[\]】]', ' ', base)
    base = re.sub(r'\s+', ' ', base).strip(' -–—:')
    if not base: return None, UNCLEAR
    alternatives = [part.strip(' -:') for part in re.split(r'\s+[|/]\s+', base)]
    for inner in re.findall(r'\(([^()]+)\)', base):
        if not YEAR_TAG.fullmatch('(' + inner + ')') and season_marker(inner)[1] is None:
            alternatives.append(inner.strip())
    stripped = [re.sub(r'\s*\((?!\s*(?:19|20)\d{2}\s*\))[^()]*\)', ' ', alt) for alt in alternatives]
    found = []
    for alt in alternatives + stripped:
        alt = re.sub(r'\s+', ' ', alt).strip(' -:')
        if alt and alt not in found: found.append(alt)
    return ParsedRelease(tuple(found), season, episode, text), None

def parse_release(title):
    """One-episode release, or None when it is a batch, range, special or unclear."""
    return read_release(title)[0]

def marker_seasons(text, *, roman=False):
    """Every season number written as a marker (S2, Season 2, 2nd Season...)."""
    found = set()
    patterns = MARKERS if roman else MARKERS[:-1]
    for pattern in patterns:
        for match in pattern.finditer(text):
            token = match.group(1)
            number = (int(token) if token.isdigit() else ORDINAL_WORDS.get(token.lower())
                      or ROMAN.get(token))
            if number: found.add(number)
    return found

# Release metadata patterns to strip from title variants
VERSION_TOKEN = re.compile(r'(?<![A-Za-z0-9])v\d{1,2}(?![A-Za-z0-9])', re.I)

RELEASE_TECH = re.compile(
    r'(?<![A-Za-z0-9])(?:\d{3,4}p|4K|UHD|WEB[ ._-]?DL|WEBRip|BluRay|'
    r'BDRip|BDMV|BD[ ._-]?Remux|Remux|HEVC|AV1|x264|x265|H264|H265|'
    r'Fixed|Proper|Repack|Uncensored|AT-X)(?![A-Za-z0-9])', re.I)

AUDIO_TAGS = re.compile(
    r'(?<![A-Za-z0-9])(?:Dual[ ._-]*Audio|Multi[ ._-]*Audio|Multi[ ._-]*Subs|'
    r'AAC(?:\s*2\.0|\s*5\.1)?|FLAC|Opus(?:\s*2\.0|\s*5\.1)?|AC3|E-AC-3|DDP)(?![A-Za-z0-9])', re.I)

PACKAGING_TAGS = re.compile(
    r'(?<![A-Za-z0-9])(?:Batch|Complete(?:[ ._-]*Season)?|CRC32|'
    r'\[[0-9a-fA-F]{8}\])(?![A-Za-z0-9])', re.I)

YEAR_TAG = re.compile(r'\(\s*(?:19|20)\d{2}\s*\)')

FOUR_DIGIT_YEAR = re.compile(r'\b(?:19|20)\d{2}\b')

# Explicit sequel markers in written titles
SEQUEL_PATTERNS = (
    re.compile(r'(?<![A-Za-z0-9])(\d{1,2})(?:st|nd|rd|th)\s+Season(?![A-Za-z])', re.I),
    re.compile(r'(?<![A-Za-z])(second|third|fourth|fifth|sixth)\s+Season(?![A-Za-z])', re.I),
    re.compile(r'(?<![A-Za-z])Season\s*(\d{1,2})(?![0-9])', re.I),
    re.compile(r'(?<![A-Za-z0-9])S(\d{1,2})(?![A-Za-z0-9])', re.I),
    re.compile(r'(?<![A-Za-z])Part\s*(\d{1,2})(?![A-Za-z0-9])', re.I),
)

SPECIAL_WORDS = re.compile(
    r'(?<![A-Za-z])(?:special|specials|sp\d*|ova|oad|ona|movie|gekijouban|recap|preview|pv|cm|extra|bonus)(?![A-Za-z])',
    re.I
)

META_PARENS = re.compile(r'\(([^()]*)\)')

METADATA_ONLY_INNER = re.compile(
    r'^(?:'
    r'BD(?:[ ._-]?Remux)?|Remux|BluRay|BDRip|BDMV|DVD|DVDRip|WEB[ ._-]?DL|WEBRip|'
    r'HEVC|AV1|AVC|x26[45]|H\.?26[45]|10[- ]?bit|8[- ]?bit|'
    r'\d{3,4}p|4K|UHD|'
    r'Dual[ ._-]*Audio|Multi[ ._-]*Subs|Multi[ ._-]*Audio|'
    r'AAC(?:\s*2\.0|\s*5\.1)?|FLAC|Opus(?:\s*2\.0|\s*5\.1)?|AC3|E-AC-3|DDP|'
    r'Batch|Complete(?:[ ._-]*Season)?|Fixed|Proper|Repack|Uncensored|AT-X|'
    r'v\d{1,2}|[0-9a-fA-F]{8}'
    r')(?:[,\s]+(?:'
    r'BD(?:[ ._-]?Remux)?|Remux|BluRay|BDRip|BDMV|DVD|DVDRip|WEB[ ._-]?DL|WEBRip|'
    r'HEVC|AV1|AVC|x26[45]|H\.?26[45]|10[- ]?bit|8[- ]?bit|'
    r'\d{3,4}p|4K|UHD|'
    r'Dual[ ._-]*Audio|Multi[ ._-]*Subs|Multi[ ._-]*Audio|'
    r'AAC(?:\s*2\.0|\s*5\.1)?|FLAC|Opus(?:\s*2\.0|\s*5\.1)?|AC3|E-AC-3|DDP|'
    r'Batch|Complete(?:[ ._-]*Season)?|Fixed|Proper|Repack|Uncensored|AT-X|'
    r'v\d{1,2}|[0-9a-fA-F]{8}'
    r'))*$', re.I)

def clean_metadata(text):
    """Strip release versions (v4), tech specs, codecs, audio, and packaging descriptors."""
    if not isinstance(text, str):
        return ""
    # Strip brackets e.g. [1080p], [Dual Audio]
    cleaned = re.sub(r'[\[【][^\]】]*[\]】]', ' ', text)
    # Strip metadata parentheticals e.g. (BD 1080p AVC Opus 2.0), (Dual Audio)
    cleaned = META_PARENS.sub(
        lambda m: '' if METADATA_ONLY_INNER.fullmatch(m.group(1).strip()) else m.group(0),
        cleaned
    )
    cleaned = VERSION_TOKEN.sub(' ', cleaned)
    cleaned = RELEASE_TECH.sub(' ', cleaned)
    cleaned = AUDIO_TAGS.sub(' ', cleaned)
    cleaned = PACKAGING_TAGS.sub(' ', cleaned)
    # Strip trailing version tokens like v4 before extension
    cleaned = re.sub(r'\s+v\d{1,2}\s*$', ' ', cleaned, flags=re.I)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' ._-:,')

    # Balance unclosed or unmatched parentheses/brackets
    while cleaned.count('(') > cleaned.count(')'):
        m = re.search(r'\([^()]*$', cleaned)
        if not m: break
        cleaned = cleaned[:m.start()].strip()
    while cleaned.count(')') > cleaned.count('('):
        cleaned = re.sub(r'\)[^()]*$', '', cleaned).strip()
    while cleaned.count('[') > cleaned.count(']'):
        m = re.search(r'\[[^\[\]]*$', cleaned)
        if not m: break
        cleaned = cleaned[:m.start()].strip()
    while cleaned.count(']') > cleaned.count('['):
        cleaned = re.sub(r'\][^\[\]]*$', '', cleaned).strip()

    cleaned = re.sub(r'[:\-\s]+$', '', cleaned).strip()
    return cleaned

def detect_explicit_sequel_marker(text):
    """Return (base_text, season_number or None) if text contains an explicit sequel marker."""
    for pattern in SEQUEL_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        token = match.group(1)
        number = (int(token) if token.isdigit() else ORDINAL_WORDS.get(token.lower())
                  or ROMAN.get(token.upper()))
        if number:
            base = text[:match.start()] + ' ' + text[match.end():]
            base = re.sub(r'\(\s*\)', ' ', base)
            base = re.sub(r'\s+', ' ', base).strip(' -:,')
            return base, number
    return text.strip(), None

EP = re.compile(
    r'(?<![A-Za-z0-9])S(?P<season>\d{1,2})[ ._-]*E(?P<season_ep>\d{1,4})(?!\d)'
    r'|\s+-\s+(?P<dash_ep>\d{1,4})(?:v\d+)?(?!\d)'
    r'|\b(?:ep|episode)[ ._-]*(?P<word_ep>\d{1,4})\b', re.I)

PREFIX = re.compile(r'^\s*(?:\[[^]]+\]\s*)+')

SEASON_TOKEN = re.compile(r'(?<![A-Za-z0-9])S\d{1,2}(?![A-Za-z0-9])', re.I)

SEASON_WORD = re.compile(r'\bSeason[ ._-]*S?\d{1,2}\b', re.I)

# Narrower than RELEASE_TECH (no Repack/Uncensored/AT-X): where a written title ends.
TITLE_END_TECH = re.compile(
    r'(?<![A-Za-z0-9])(?:\d{3,4}p|4K|UHD|WEB[ ._-]?DL|WEBRip|BluRay|'
    r'BDRip|BDMV|BD[ ._-]?Remux|Remux|HEVC|AV1|x264|x265|H264|H265|'
    r'Fixed|Proper)(?![A-Za-z0-9])', re.I)

META_GROUP = re.compile(r'\(([^()]*)\)')

TRAIL = re.compile(r'\s*(?:\[[^]]+\]\s*)+$')

METADATA_ONLY = re.compile(
    r'^(?:'
    r'(?:Batch|Season[ ._-]*S?\d{1,2}|Complete(?:[ ._-]*Season)?|'
    r'Dual[ ._-]*Audio|Multi[ ._-]*Subs|Multi[ ._-]*Audio|'
    r'\d{3,4}p|4K|AV1|HEVC|x26[45]|'
    r'BD(?:[ ._-]?Remux)?|Remux|BDRip|WEB[ ._-]?DL|Fixed|Proper)'
    r'(?:[,\s]+(?:Batch|Season[ ._-]*S?\d{1,2}|Complete(?:[ ._-]*Season)?|'
    r'Dual[ ._-]*Audio|Multi[ ._-]*Subs|Multi[ ._-]*Audio|'
    r'\d{3,4}p|4K|AV1|HEVC|x26[45]|'
    r'BD(?:[ ._-]?Remux)?|Remux|BDRip|WEB[ ._-]?DL|Fixed|Proper))*'
    r')$', re.I)

def release_title(raw):
    """Return series-title search aliases and an advisory episode number.

    Extract release labels *before* querying AniDB/TVDB. Episode and season
    hints from a filename are NOT canonical identity or download authority.
    In particular do not search standalone "(Batch)" or "(Season S2)" tags.
    """
    if not isinstance(raw, str) or not raw.strip():
        return [], None
    # Only the primary title determines base release aliases; pipe alternatives
    # are handled as distinct sequel/alternate aliases.
    primary = raw.split(" | ", 1)[0].strip() if " | " in raw else raw.strip()
    parsed = parse_release(primary)
    if parsed is not None:
        aliases = []
        for written in parsed.titles:
            cleaned_written = clean_metadata(written)
            base, number = season_marker(cleaned_written, roman=False)
            candidates_to_add = (cleaned_written, base) if (number is not None and base != cleaned_written) else (cleaned_written,)
            for alias in candidates_to_add:
                cleaned = re.sub(r'[\(\[\{][^\)\]\}]*$', '', alias).strip()
                cleaned = re.sub(r'[\(\[\{:\-\s]+$', '', cleaned).strip()
                cleaned = re.sub(r'[\)\]\}]+$', '', cleaned).strip()
                if cleaned and not re.fullmatch(r'^[A-Za-z]?\d{6,}$', cleaned) and cleaned not in aliases:
                    aliases.append(cleaned)
        if aliases:
            return aliases, parsed.episode

    value = PREFIX.sub('', primary).strip()
    episode_match = EP.search(value)
    episode = (next((int(x) for x in (
        episode_match.group('season_ep'), episode_match.group('dash_ep'),
        episode_match.group('word_ep')) if x is not None), None)
        if episode_match else None)

    slash_parts = [sp.strip() for sp in re.split(r'\s+/\s+', value) if sp.strip()]
    if not slash_parts:
        slash_parts = [value]

    raw_aliases = []
    for sp in slash_parts:
        ep_m = EP.search(sp)
        boundaries = [m.start() for m in (ep_m,
                                         SEASON_TOKEN.search(sp),
                                         SEASON_WORD.search(sp),
                                         TITLE_END_TECH.search(sp)) if m]
        title = (sp[:min(boundaries)] if boundaries else sp).strip(' ._-')
        title = TRAIL.sub('', title).strip()
        title = clean_metadata(title)
        if not title:
            continue

        base, num = detect_explicit_sequel_marker(title)
        if num is not None and base != title:
            raw_aliases.extend([title, base])
        else:
            raw_aliases.append(title)

        if YEAR_TAG.search(title):
            unyeared = re.sub(r'\s+', ' ', YEAR_TAG.sub(' ', title)).strip(' -:,')
            if unyeared:
                raw_aliases.append(unyeared)

        without_parens = META_GROUP.sub('', title).strip(' ._-')
        if without_parens and without_parens != title:
            raw_aliases.append(without_parens)

    aliases = []
    for item in raw_aliases:
        cleaned = item.strip()
        while cleaned.count('(') > cleaned.count(')'):
            m = re.search(r'\([^()]*$', cleaned)
            if not m: break
            cleaned = cleaned[:m.start()].strip()
        while cleaned.count(')') > cleaned.count('('):
            cleaned = re.sub(r'\)[^()]*$', '', cleaned).strip()
        while cleaned.count('[') > cleaned.count(']'):
            m = re.search(r'\[[^\[\]]*$', cleaned)
            if not m: break
            cleaned = cleaned[:m.start()].strip()
        while cleaned.count(']') > cleaned.count('['):
            cleaned = re.sub(r'\][^\[\]]*$', '', cleaned).strip()
        cleaned = re.sub(r'[:\-\s]+$', '', cleaned).strip()
        if not cleaned:
            continue
        if re.fullmatch(r'^[A-Za-z]?\d{6,}$', cleaned) or re.fullmatch(r'^[0-9a-fA-F]{16,}$', cleaned):
            continue
        if cleaned not in aliases:
            aliases.append(cleaned)
    return aliases, episode

def norm(s):
    return ''.join(ch for ch in unicodedata.normalize('NFKC', s).casefold() if ch.isalnum())
