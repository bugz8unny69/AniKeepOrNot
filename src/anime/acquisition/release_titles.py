"""Match a read release against the anime's catalog titles, and build searches.

Reading the release name itself lives in src/anime/release_names.py. This
module decides whether a read title is this anime and season, and which
Prowlarr queries to send. A title alone never authorizes acquisition.
"""
from dataclasses import dataclass
import re
from src.anime.release_names import YEAR_TAG, norm, season_marker


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
            elif tvdb_season > 1 and YEAR_TAG.search(title):
                # A catalog title may contain a year that torrent releases omit.
                # Treat the yearless name as a franchise alias: require a season
                # marker (or absolute episode number), not a bare sequel episode.
                plain = norm(YEAR_TAG.sub(' ', title))
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
        unyeared = norm(YEAR_TAG.sub(' ', base))
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
        if n is not None and (xx, yy) == (tvdb_season, n): return True
        return xx in titles.seasons and (yy == a or (n is not None and yy == n)) or (best == 'exact' and xx == 1
                                                          and not titles.seasons and yy == a)
    episode = parsed.episode
    if best == 'franchise':
        return absolute is not None and absolute != a and episode == absolute
    accepted = {a}
    if absolute is not None: accepted.add(absolute)
    if marker is not None and n is not None and marker == tvdb_season: accepted.add(n)
    return episode in accepted


def search_queries(catalog_titles, titles, target, *, season=None, english_title=None, prefer_audio=False):
    """A few indexer queries that together reach common naming styles while bounded at 6."""
    from src.anime.acquisition.prowlarr_search import search_aliases
    aliases = tuple(re.sub(r'\s+', ' ', YEAR_TAG.sub(' ', alias)).strip()
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

    # The catalog already supplies a candidate absolute number for the verified
    # TVDB placement. Give its distinct naming format one slot rather than
    # losing it behind lower-priority synonyms after the six-query limit.
    # Search wording is only a hint; matches() still checks episode identity.
    bases = sorted({base for base, _ in season_bases(catalog_titles)})
    absolute_query = None
    if (target.absolute_episode is not None
            and target.absolute_episode != target.anidb_episode):
        absolute_base = bases[0] if bases else effective_scene_base
        absolute_query = '%s %02d' % (absolute_base, target.absolute_episode)

    # English only when AniDB gave one; a second romanized alias is just a synonym.
    eng = None
    if type(english_title) is str and english_title.strip():
        wanted = norm(YEAR_TAG.sub(' ', english_title))
        eng = next((alias for alias in aliases[1:] if norm(alias) == wanted), None)
    has_english = eng is not None
    synonyms = tuple(alias for alias in aliases[1:] if alias != eng)
    if has_english:
        eng_base, eng_marker = season_marker(eng)
        eng_scene_base = eng_base if eng_marker is not None else eng

    has_tvdb = target.tvdb_episode is not None

    if prefer_audio and has_english:
        # Prioritize English scene queries and audio hints when dual-audio is required
        if has_tvdb:
            queries.append('%s S%02dE%02d' % (eng_scene_base, current_season, target.tvdb_episode))
            queries.append('%s S%02dE%02d Dual-Audio' % (eng_scene_base, current_season, target.tvdb_episode))
        queries.append('%s %02d' % (eng, target.anidb_episode))
        queries.append('%s %02d' % (main, target.anidb_episode))
        if has_tvdb:
            queries.append('%s S%02dE%02d' % (effective_scene_base, current_season, target.tvdb_episode))
        if absolute_query:
            queries.append(absolute_query)
        for syn in synonyms:
            queries.append('%s %02d' % (syn, target.anidb_episode))
        if has_tvdb:
            queries.append('%s S%02dE%02d Dual-Audio' % (effective_scene_base, current_season, target.tvdb_episode))
    else:
        # 1. Primary queries for main title: traditional AniDB number and scene SxxEyy
        queries.append('%s %02d' % (main, target.anidb_episode))
        if has_tvdb:
            queries.append('%s S%02dE%02d' % (effective_scene_base, current_season, target.tvdb_episode))
        if absolute_query:
            queries.append(absolute_query)

        # 2. AniDB English title queries:
        if has_english:
            queries.append('%s %02d' % (eng, target.anidb_episode))
            if has_tvdb:
                queries.append('%s S%02dE%02d' % (eng_scene_base, current_season, target.tvdb_episode))

        # 3. Broad fallback for additional aliases (e.g. short synonym "Saikyosoubi 01"):
        for syn in synonyms:
            queries.append('%s %02d' % (syn, target.anidb_episode))

        # 4. Audio-aware query variants when prefer_audio (Japanese + English required)
        if prefer_audio:
            if has_tvdb:
                queries.append('%s S%02dE%02d Dual-Audio' % (effective_scene_base, current_season, target.tvdb_episode))
            queries.append('%s %02d Dual-Audio' % (main, target.anidb_episode))

        # 5. Scene queries for additional synonyms:
        if has_tvdb:
            for syn in synonyms:
                s_base, s_marker = season_marker(syn)
                s_scene_base = s_base if s_marker is not None else syn
                queries.append('%s S%02dE%02d' % (s_scene_base, current_season, target.tvdb_episode))

    # 7. For sequels with season bases (e.g. "Kusuriya no Hitorigoto 2nd Season" -> base "Kusuriya no Hitorigoto")
    for base in bases[:1]:
        queries.append('%s %02d' % (base, target.anidb_episode))
        if has_tvdb:
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


def batch_search_queries(catalog_titles, season, *, english_title=None, prefer_audio=False):
    """Bounded search queries for season packs (WI-05)."""
    from src.anime.acquisition.prowlarr_search import search_aliases
    aliases = tuple(re.sub(r'\s+', ' ', YEAR_TAG.sub(' ', alias)).strip()
                    for alias in search_aliases(catalog_titles, english_title=english_title))
    if not aliases:
        return ()

    current_season = season if season is not None and season > 0 else 1
    queries = []
    main = aliases[0]
    main_base, main_marker = season_marker(main)
    effective_base = main_base if main_marker is not None else main

    eng = None
    if type(english_title) is str and english_title.strip():
        wanted = norm(YEAR_TAG.sub(' ', english_title))
        eng = next((alias for alias in aliases[1:] if norm(alias) == wanted), None)
    has_english = eng is not None
    eng_base = None
    if has_english:
        eb, em = season_marker(eng)
        eng_base = eb if em is not None else eng

    if prefer_audio:
        if has_english:
            queries.append(f"{eng_base} S{current_season:02d} Dual Audio")
            queries.append(f"{eng_base} Season {current_season} Dual Audio")
        queries.append(f"{effective_base} S{current_season:02d} Dual Audio")
        queries.append(f"{effective_base} Season {current_season} Dual Audio")
        queries.append(f"{effective_base} S{current_season:02d}")
        queries.append(f"{effective_base} Batch")
    else:
        queries.append(f"{effective_base} S{current_season:02d}")
        queries.append(f"{effective_base} Season {current_season}")
        queries.append(f"{effective_base} Batch")
        if has_english:
            queries.append(f"{eng_base} S{current_season:02d}")
            queries.append(f"{eng_base} Season {current_season}")

    # Deduplicate while preserving order and bound at 6 queries
    seen = set()
    result = []
    for q in queries:
        cleaned = re.sub(r'\s+', ' ', q).strip()
        if cleaned and cleaned not in seen and 1 <= len(cleaned) <= 256:
            seen.add(cleaned)
            result.append(cleaned)
            if len(result) >= 6:
                break
    return tuple(result)
