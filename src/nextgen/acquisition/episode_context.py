"""Authoritative, frozen episode-search context for backfill discovery.

Separates episode identity resolution from torrent candidate discovery. Reuses
saved selections and verified recognized episodes without reconstructing identity
from scratch on every search or fallback.
"""
from dataclasses import dataclass
import json
import sqlite3

from src.nextgen.sonarr_episode import VerifiedEpisode, SonarrEpisodeMismatch


def _positive(value):
    return type(value) is int and value > 0


@dataclass(frozen=True)
class EpisodeSearchContext(VerifiedEpisode):
    anidb_id: int = 0
    anidb_episode: int = 0
    titles: tuple[str, ...] = ()
    english_title: str | None = None
    absolute_episode: int | None = None
    status: str = 'SEARCHABLE'
    verified: bool = True
    provenance: str = 'SAVED_SELECTION'

    def __post_init__(self):
        super().__post_init__()
        if not _positive(self.anidb_id):
            raise SonarrEpisodeMismatch("Verified AniDB ID is invalid.")
        if not _positive(self.anidb_episode):
            raise SonarrEpisodeMismatch("Verified AniDB episode number is invalid.")
        if not self.titles or not all(type(t) is str and t.strip() for t in self.titles):
            raise SonarrEpisodeMismatch("Episode search context requires at least one anime title.")
        if self.status != 'SEARCHABLE':
            raise SonarrEpisodeMismatch("Episode search context is not searchable.")
        if type(self.verified) is not bool:
            raise SonarrEpisodeMismatch("Invalid verified flag.")
        if type(self.provenance) is not str or not self.provenance.strip():
            raise SonarrEpisodeMismatch("Invalid context provenance.")


def load_authoritative_episode_context(db, plan_id, episode):
    """Load authoritative, frozen episode search context from stored evidence.

    Zero network requests. Returns EpisodeSearchContext if verified identity is
    established for this plan and episode, or None if context is missing/conflicting.
    """
    if type(plan_id) is not int or plan_id <= 0 or type(episode) is not int or episode <= 0:
        return None

    try:
        # 1. Plan metadata
        plan_row = db.execute(
            "SELECT anidb_id, tvdb_series_id, tvdb_season FROM trial_plans WHERE plan_id=?",
            (plan_id,)
        ).fetchone()
        if plan_row is None:
            return None
        aid, tid, plan_season = plan_row

        # 2. Check saved selection first (provenance: SAVED_SELECTION)
        sel_row = db.execute(
            "SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM sonarr_episode_selections "
            "WHERE plan_id=? AND anidb_episode_number=?",
            (plan_id, episode)
        ).fetchone()

        # 3. Check recognized episodes (provenance: RECOGNIZED_EPISODE)
        rec_row = db.execute(
            "SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM recognized_episodes "
            "WHERE anidb_id=? AND anidb_episode_number=? AND tvdb_series_id=?",
            (aid, episode, tid)
        ).fetchone()

        # 4. Check release_verifications as fallback for verified placement
        ver_row = None
        if sel_row is None and rec_row is None:
            ver_row = db.execute(
                "SELECT tvdb_season, tvdb_episode, tvdb_episode_id FROM release_verifications "
                "WHERE anidb_id=? AND anidb_episode_number=? AND tvdb_series_id=? AND tvdb_order='official' "
                "ORDER BY verification_id DESC LIMIT 1",
                (aid, episode, tid)
            ).fetchone()

        if sel_row and rec_row and sel_row != rec_row:
            # Placement conflict between stored selection and recognized episode
            return None

        placement = sel_row or rec_row or ver_row
        if placement is None:
            return None

        tvdb_season, tvdb_episode, tvdb_episode_id = placement
        if not _positive(tvdb_episode_id) or type(tvdb_season) is not int or tvdb_season < 0 or not _positive(tvdb_episode):
            return None

        provenance = 'SAVED_SELECTION' if sel_row else ('RECOGNIZED_EPISODE' if rec_row else 'RELEASE_VERIFICATION')

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

        # Aliases from recognized_aliases
        alias_rows = db.execute(
            "SELECT alias_key FROM recognized_aliases WHERE anidb_id=? AND tvdb_series_id=? AND tvdb_season=? "
            "ORDER BY rowid",
            (aid, tid, tvdb_season)
        ).fetchall()
        for (alias_key,) in alias_rows:
            add_title(alias_key)

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
            return None

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
            verified=True,
            provenance=provenance,
        )
    except (sqlite3.Error, SonarrEpisodeMismatch, ValueError, TypeError):
        return None
