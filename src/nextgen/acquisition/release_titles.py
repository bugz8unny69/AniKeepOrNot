"""Read one episode release title the way different release groups write it.

Covers the common styles seen on anime indexers, for example:

    [SubsPlease] Title - 05 (1080p) [ABCD1234].mkv
    [Erai-raws] Title 2nd Season - 05 [1080p CR WEB-DL AVC AAC][MultiSub]
    [Judas] Title - S02E05 [1080p][HEVC x265 10bit]
    Title S02E05 1080p CR WEB-DL AAC2.0 H 264-VARYG
    The.Title.S02E05.1080p.WEB.h264-GROUP
    [Group] Title S2 - 05 / Title Season 2 - 05 / Title II - 05
    [Group] Romaji Title | English Title - 05 (1080p)

Batch/range declarations and non-episode media are rejected.
An explicit Season 0 declaration still needs a verified AniDB-to-TVDB episode
mapping; a torrent title alone never authorizes acquisition.
"""
from dataclasses import dataclass
import re

from src.nextgen.providers.parsing import norm

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
YEAR = re.compile(r'\(\s*(?:19|20)\d{2}\s*\)')
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
        if not YEAR.fullmatch('(' + inner + ')') and season_marker(inner)[1] is None:
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


@dataclass(frozen=True)
class AnimeTitles:
    """The verified anime's AniDB titles, prepared for comparison."""
    exact: frozenset            # every title, normalized
    marked: frozenset           # (normalized base, season) of titles with a season marker
    franchise: frozenset        # normalized bases of the marked titles
    seasons: frozenset          # season numbers this anime is known by
    tvdb_season: int = 1

    @classmethod
    def build(cls, titles, tvdb_season):
        exact, marked = set(), set()
        for title in titles:
            if type(title) is not str or not title.strip(): continue
            exact.add(norm(title))
            base, number = season_marker(title)
            if number is not None and norm(base):
                marked.add((norm(base), number))
            elif tvdb_season > 1 and YEAR.search(title):
                # A catalog title may contain a year that torrent releases omit.
                # Treat the yearless name as a franchise alias: require a season
                # marker (or absolute episode number), not a bare sequel episode.
                plain = norm(YEAR.sub(' ', title))
                if plain:
                    marked.add((plain, tvdb_season))
        seasons = {number for _, number in marked}
        if type(tvdb_season) is int and tvdb_season > 1: seasons.add(tvdb_season)
        exact.discard('')
        season_num = tvdb_season if type(tvdb_season) is int and tvdb_season >= 0 else 1
        return cls(frozenset(exact), frozenset(marked),
                   frozenset(base for base, _ in marked), frozenset(seasons),
                   tvdb_season=season_num)

    def level(self, written):
        """'exact', 'season', 'franchise' or None for one written title alternative."""
        full = norm(written)
        base, number = season_marker(written)
        unyeared = norm(YEAR.sub(' ', base))
        if full in self.exact and full not in self.franchise:
            return 'exact', number
        if number is not None and number in self.seasons and (
                (norm(base), number) in self.marked or (unyeared, number) in self.marked
                or norm(base) in self.exact or unyeared in self.exact
                or norm(base) in self.franchise or unyeared in self.franchise):
            return 'season', number
        if number is None and (full in self.franchise or unyeared in self.franchise):
            return 'franchise', None
        return None, None


def matches(parsed, titles, target, tvdb_season):
    """Whether a parsed release is exactly the target episode of this anime."""
    if parsed is None: return False
    best = None
    for written in parsed.titles:
        level, number = titles.level(written)
        if level == 'exact' or level == 'season' and best != 'exact' or level == 'franchise' and best is None:
            best, marker = level, number
    if best is None: return False
    a, n, absolute = target.anidb_episode, target.tvdb_episode, target.absolute_episode
    if parsed.season is not None:
        xx, yy = parsed.season, parsed.episode
        if (xx, yy) == (tvdb_season, n): return True
        return xx in titles.seasons and yy in (a, n) or (best == 'exact' and xx == 1
                                                          and not titles.seasons and yy == a)
    episode = parsed.episode
    if best == 'franchise':
        return absolute is not None and absolute != a and episode == absolute
    accepted = {a}
    if absolute is not None: accepted.add(absolute)
    if marker is not None and marker == tvdb_season: accepted.add(n)
    return episode in accepted


def search_queries(catalog_titles, titles, target, *, season=None, english_title=None, prefer_audio=False):
    """A few indexer queries that together reach common naming styles while bounded at 6."""
    from src.nextgen.acquisition.prowlarr_search import search_aliases
    aliases = tuple(re.sub(r'\s+', ' ', YEAR.sub(' ', alias)).strip()
                    for alias in search_aliases(catalog_titles, english_title=english_title))
    if not aliases:
        return ()

    current_season = season if season is not None else getattr(titles, 'tvdb_season', 1)
    if current_season == 1 and titles.seasons:
        current_season = min(titles.seasons)

    queries = []

    main = aliases[0]
    main_base, main_marker = season_marker(main)
    effective_scene_base = main_base if main_marker is not None else main

    # English only when AniDB gave one; a second romanized alias is just a synonym.
    eng = None
    if type(english_title) is str and english_title.strip():
        wanted = norm(YEAR.sub(' ', english_title))
        eng = next((alias for alias in aliases[1:] if norm(alias) == wanted), None)
    has_english = eng is not None
    synonyms = tuple(alias for alias in aliases[1:] if alias != eng)
    if has_english:
        eng_base, eng_marker = season_marker(eng)
        eng_scene_base = eng_base if eng_marker is not None else eng

    if prefer_audio and has_english:
        # Prioritize English scene queries and audio hints when dual-audio is required
        queries.append('%s S%02dE%02d' % (eng_scene_base, current_season, target.tvdb_episode))
        queries.append('%s S%02dE%02d Dual-Audio' % (eng_scene_base, current_season, target.tvdb_episode))
        queries.append('%s %02d' % (eng, target.anidb_episode))
        queries.append('%s %02d' % (main, target.anidb_episode))
        queries.append('%s S%02dE%02d' % (effective_scene_base, current_season, target.tvdb_episode))
        for syn in synonyms:
            queries.append('%s %02d' % (syn, target.anidb_episode))
        queries.append('%s S%02dE%02d Dual-Audio' % (effective_scene_base, current_season, target.tvdb_episode))
    else:
        # 1. Primary queries for main title: traditional AniDB number and scene SxxEyy
        queries.append('%s %02d' % (main, target.anidb_episode))
        queries.append('%s S%02dE%02d' % (effective_scene_base, current_season, target.tvdb_episode))

        # 2. AniDB English title queries:
        if has_english:
            queries.append('%s %02d' % (eng, target.anidb_episode))
            queries.append('%s S%02dE%02d' % (eng_scene_base, current_season, target.tvdb_episode))

        # 3. Broad fallback for additional aliases (e.g. short synonym "Saikyosoubi 01"):
        for syn in synonyms:
            queries.append('%s %02d' % (syn, target.anidb_episode))

        # 4. Audio-aware query variants when prefer_audio (Japanese + English required)
        if prefer_audio:
            queries.append('%s S%02dE%02d Dual-Audio' % (effective_scene_base, current_season, target.tvdb_episode))
            queries.append('%s %02d Dual-Audio' % (main, target.anidb_episode))

        # 5. Scene queries for additional synonyms:
        for syn in synonyms:
            s_base, s_marker = season_marker(syn)
            s_scene_base = s_base if s_marker is not None else syn
            queries.append('%s S%02dE%02d' % (s_scene_base, current_season, target.tvdb_episode))

    # 7. For sequels with season bases (e.g. "Kusuriya no Hitorigoto 2nd Season" -> base "Kusuriya no Hitorigoto")
    bases = sorted({base for base, _ in season_bases(catalog_titles)})
    for base in bases[:1]:
        queries.append('%s %02d' % (base, target.anidb_episode))
        if target.absolute_episode and target.absolute_episode != target.anidb_episode:
            queries.append('%s %02d' % (base, target.absolute_episode))
        queries.append('%s S%02dE%02d' % (base, current_season, target.tvdb_episode))

    return tuple(dict.fromkeys(q for q in queries if 1 <= len(q) <= 256))[:6]


def season_bases(catalog_titles):
    """(written base, season) for Latin-script titles that carry a season marker."""
    found = []
    for title in catalog_titles:
        if type(title) is not str or not title.isascii(): continue
        base, number = season_marker(title)
        if number is not None and base: found.append((base, number))
    return found
