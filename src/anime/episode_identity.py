"""A verified TVDB episode identity, used by episode discovery and Keep."""
from dataclasses import dataclass


class EpisodeIdentityInvalid(ValueError):
    """The verified TVDB episode identity is incomplete or not valid."""


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
            raise EpisodeIdentityInvalid("Verified TVDB episode identity is invalid.")
        has_placement = (self.tvdb_episode_id is not None or self.tvdb_season is not None
                         or self.tvdb_episode is not None)
        if has_placement:
            if (not _positive(self.tvdb_episode_id)
                    or type(self.tvdb_season) is not int or self.tvdb_season < 0
                    or not _positive(self.tvdb_episode)):
                raise EpisodeIdentityInvalid("Verified TVDB episode identity is invalid.")
