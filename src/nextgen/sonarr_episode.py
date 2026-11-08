"""Match a verified TVDB episode to its Sonarr series and episode IDs (Keep)."""
from src.nextgen.episode_identity import EpisodeIdentityInvalid, _positive


class SonarrEpisodeMismatch(EpisodeIdentityInvalid):
    """Sonarr does not uniquely identify the verified TVDB episode."""


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
