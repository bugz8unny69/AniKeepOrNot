"""Authoritative, frozen episode-search context for backfill discovery.

Separates episode identity resolution from torrent candidate discovery. Reuses
saved selections and verified recognized episodes without reconstructing identity
from scratch on every search or fallback.
"""
from dataclasses import dataclass
import json

from src.anime.episode_identity import VerifiedEpisode
from src.anime.episode_identity import EpisodeIdentityInvalid


def _positive(value):
    return type(value) is int and value > 0


class EpisodeContextMissing(LookupError):
    reason_code = 'CONTEXT_MISSING'


class EpisodeContextConflict(ValueError):
    reason_code = 'EPISODE_CONTEXT_CONFLICT'

    def __init__(self, left_name, left, right_name, right):
        self.failed_check = (
            'src/anime/acquisition/episode_context.py:20 '
            'load_authoritative_episode_context CONTEXT_CONFLICT'
        )
        self.context_left = _placement_fact(left_name, left)
        self.context_right = _placement_fact(right_name, right)
        super().__init__('Persisted episode identities disagree.')


def _valid_placement(row):
    return (row is not None and len(row) == 3
            and type(row[0]) is int and 0 <= row[0] <= 9999
            and type(row[1]) is int and 1 <= row[1] <= 99999
            and type(row[2]) is int and 1 <= row[2] <= 999999999999)


def _placement_fact(name, row):
    if not _valid_placement(row):
        return name + ':INVALID'
    return f'{name}:S{row[0]}E{row[1]}#{row[2]}'


@dataclass(frozen=True)
class EpisodeSearchContext(VerifiedEpisode):
    anidb_id: int = 0
    anidb_episode: int = 0
    titles: tuple[str, ...] = ()
    english_title: str | None = None
    absolute_episode: int | None = None
    status: str = 'SEARCHABLE'
    verified: bool = False
    provenance: str = 'SAVED_SELECTION'
    verification_id: int | None = None

    def __post_init__(self):
        super().__post_init__()
        if not _positive(self.anidb_id):
            raise EpisodeIdentityInvalid("Verified AniDB ID is invalid.")
        if not _positive(self.anidb_episode):
            raise EpisodeIdentityInvalid("Verified AniDB episode number is invalid.")
        if not self.titles or not all(type(t) is str and t.strip() for t in self.titles):
            raise EpisodeIdentityInvalid("Episode search context requires at least one anime title.")
        if self.status != 'SEARCHABLE':
            raise EpisodeIdentityInvalid("Episode search context is not searchable.")
        if type(self.verified) is not bool:
            raise EpisodeIdentityInvalid("Invalid verified flag.")
        if self.provenance not in ('SAVED_SELECTION', 'RECOGNIZED_EPISODE', 'RELEASE_VERIFICATION'):
            raise EpisodeIdentityInvalid("Invalid context provenance.")
        if self.verification_id is not None and not _positive(self.verification_id):
            raise EpisodeIdentityInvalid("Invalid verification reference.")
        if self.verified != (self.verification_id is not None):
            raise EpisodeIdentityInvalid("Verified context requires independent evidence.")
        if self.provenance == 'RELEASE_VERIFICATION' and not self.verified:
            raise EpisodeIdentityInvalid("Release verification provenance needs evidence.")
        if self.absolute_episode is not None and not _positive(self.absolute_episode):
            raise EpisodeIdentityInvalid("Invalid absolute episode.")


def load_authoritative_episode_context(db, plan_id, episode):
    """Load authoritative, frozen episode search context from stored evidence.

    Zero provider requests. Only EpisodeContextMissing permits legacy catalog
    fallback; conflicts fail closed and database errors propagate unchanged.
    """
    if type(plan_id) is not int or plan_id <= 0 or type(episode) is not int or episode <= 0:
        raise EpisodeContextMissing('Invalid episode coordinates.')

    # 1. Plan metadata
    plan_row = db.execute(
        "SELECT anidb_id, tvdb_series_id, tvdb_season FROM user_evaluation_plans WHERE plan_id=?",
        (plan_id,)
    ).fetchone()
    if plan_row is None:
        raise EpisodeContextMissing('No saved plan.')
    aid, tid, plan_season = plan_row

    # 2. Check saved selection first (provenance: SAVED_SELECTION)
    sel_row = db.execute(
        "SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM episode_release_selections "
        "WHERE plan_id=? AND anidb_episode_number=?",
        (plan_id, episode)
    ).fetchone()

    # 3. Check recognized episodes (provenance: RECOGNIZED_EPISODE)
    rec_row = db.execute(
        "SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM recognized_episodes "
        "WHERE anidb_id=? AND anidb_episode_number=? AND tvdb_series_id=?",
        (aid, episode, tid)
    ).fetchone()

    # A selected torrent alone is NOT independent release verification.
    ver_row = db.execute(
        "SELECT verification_id, tvdb_season, tvdb_episode, tvdb_episode_id "
        "FROM release_verifications "
        "WHERE anidb_id=? AND anidb_episode_number=? AND tvdb_series_id=? "
        "AND tvdb_order='official' ORDER BY verification_id DESC LIMIT 1",
        (aid, episode, tid)
    ).fetchone()
    selected = tuple(sel_row) if sel_row is not None else None
    recognized = tuple(rec_row) if rec_row is not None else None
    independently_verified = tuple(ver_row[1:]) if ver_row is not None else None
    evidence = [('selection', selected), ('recognized', recognized),
                ('verification', independently_verified)]
    present = [(name, row) for name, row in evidence if row is not None]
    if not present:
        raise EpisodeContextMissing('No saved episode placement.')
    for name, row in present:
        if not _valid_placement(row):
            raise EpisodeContextConflict(name, row, name, None)
    first_name, placement = present[0]
    for name, row in present[1:]:
        if row != placement:
            raise EpisodeContextConflict(first_name, placement, name, row)
    tvdb_season, tvdb_episode, tvdb_episode_id = placement
    provenance = ('SAVED_SELECTION' if selected is not None else
                  'RECOGNIZED_EPISODE' if recognized is not None else
                  'RELEASE_VERIFICATION')
    verification_id = ver_row[0] if ver_row is not None else None

    # 5. Collect anime titles and aliases
    titles = []
    seen = set()

    def add_title(t):
        if t and type(t) is str:
            clean = t.strip()
            if clean and clean not in seen:
                seen.add(clean)
                titles.append(clean)

    # Primary title from identified_anime
    anime_row = db.execute("SELECT title FROM identified_anime WHERE anidb_id=?", (aid,)).fetchone()
    if anime_row:
        add_title(anime_row[0])

    # Display title from recognized_seasons
    season_row = db.execute(
        "SELECT display_title FROM recognized_seasons WHERE anidb_id=? AND tvdb_series_id=? AND tvdb_season=?",
        (aid, tid, tvdb_season)
    ).fetchone()
    if season_row:
        add_title(season_row[0])

    # recognized_aliases.alias_key is norm(title): punctuation and whitespace
    # are intentionally stripped for identity lookups. It is NOT a display
    # alias and must never be sent to Prowlarr as a query. Only collect
    # readable original titles from the metadata and saved identity snapshot.

    # English and romaji titles from anime_title_metadata
    meta_row = db.execute(
        "SELECT english_title, romaji_title FROM anime_title_metadata WHERE anidb_id=?",
        (aid,)
    ).fetchone()
    english_title = meta_row[0] if meta_row and meta_row[0] else None
    if meta_row:
        add_title(meta_row[0])
        add_title(meta_row[1])

    # Supplementary titles from identity_results snapshot
    info = db.execute(
        "SELECT series_json FROM identity_results WHERE anidb_id=? "
        "ORDER BY validated_at_ms DESC LIMIT 1",
        (aid,)
    ).fetchone()
    if info and info[0]:
        try:
            snapshot = json.loads(info[0])
            anime = snapshot.get('anime', {})
            add_title(anime.get('title'))
            if not english_title and anime.get('english_title'):
                english_title = anime.get('english_title')
            add_title(anime.get('english_title'))
            extra_titles = anime.get('titles') or []
            if isinstance(extra_titles, (list, tuple)):
                for t in extra_titles:
                    add_title(t)
        except (ValueError, TypeError):
            pass

    if not titles:
        raise EpisodeContextMissing('No persisted search titles.')

    # 6. Calculate absolute_episode
    absolute_episode = None
    if tvdb_season == 1:
        absolute_episode = tvdb_episode
    elif tvdb_season > 1:
        earlier = db.execute(
            "SELECT count(*) FROM recognized_episodes WHERE tvdb_series_id=? AND tvdb_season>=1 AND tvdb_season<?",
            (tid, tvdb_season)
        ).fetchone()[0]
        if earlier > 0:
            absolute_episode = earlier + tvdb_episode

    return EpisodeSearchContext(
        tvdb_series_id=tid,
        tvdb_episode_id=tvdb_episode_id,
        tvdb_season=tvdb_season,
        tvdb_episode=tvdb_episode,
        anidb_id=aid,
        anidb_episode=episode,
        titles=tuple(titles),
        english_title=english_title,
        absolute_episode=absolute_episode,
        status='SEARCHABLE',
        verified=verification_id is not None,
        provenance=provenance,
        verification_id=verification_id,
    )
    # Database errors are intentionally not caught or reclassified as missing.

