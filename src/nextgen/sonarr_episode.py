"""Pure TVDB-to-Sonarr episode matching shared by discovery and Keep."""
from dataclasses import dataclass


class SonarrEpisodeMismatch(ValueError):
    """Sonarr does not uniquely identify the verified TVDB episode."""


def _positive(value):
    return type(value) is int and value > 0


@dataclass(frozen=True)
class VerifiedEpisode:
    tvdb_series_id: int
    tvdb_episode_id: int | None = None
    tvdb_season: int | None = None
    tvdb_episode: int | None = None

    def __post_init__(self):
        if not _positive(self.tvdb_series_id):
            raise SonarrEpisodeMismatch("Verified TVDB episode identity is invalid.")
        has_placement = (self.tvdb_episode_id is not None or self.tvdb_season is not None
                         or self.tvdb_episode is not None)
        if has_placement:
            if (not _positive(self.tvdb_episode_id)
                    or type(self.tvdb_season) is not int or self.tvdb_season < 0
                    or not _positive(self.tvdb_episode)):
                raise SonarrEpisodeMismatch("Verified TVDB episode identity is invalid.")


def resolve_episode(identity, series, episodes):
    """Return exact (Sonarr series ID, episode ID); never match by titles."""
    if (type(series) is not dict or type(episodes) is not list
            or not _positive(series.get('id')) or series.get('tvdbId') != identity.tvdb_series_id):
        raise SonarrEpisodeMismatch("Sonarr series does not match TVDB.")
    if identity.tvdb_episode_id is None or identity.tvdb_season is None or identity.tvdb_episode is None:
        raise SonarrEpisodeMismatch("Sonarr episode mapping is missing.")
    sid = series['id']
    matches = [
        row for row in episodes
        if type(row) is dict
        and row.get('seriesId') == sid
        and type(row.get('seasonNumber')) is int and row['seasonNumber'] == identity.tvdb_season
        and type(row.get('episodeNumber')) is int and row['episodeNumber'] == identity.tvdb_episode
    ]
    if len(matches) != 1 or not _positive(matches[0].get('id')):
        raise SonarrEpisodeMismatch("Sonarr episode mapping is missing or ambiguous.")
    match = matches[0]
    if match.get('tvdbId') not in (None, 0, identity.tvdb_episode_id):
        raise SonarrEpisodeMismatch("Sonarr episode has a different TVDB ID.")
    return sid, match['id']


def __getattr__(name):
    if name in ('EpisodeSearchContext', 'load_authoritative_episode_context'):
        from src.nextgen.acquisition.episode_context import (
            EpisodeSearchContext as _ESC,
            load_authoritative_episode_context as _LAEC,
        )
        if name == 'EpisodeSearchContext':
            return _ESC
        return _LAEC
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

