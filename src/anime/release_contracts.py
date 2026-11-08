"""Immutable declared-episode verification facts; no acquisition authorization."""
from dataclasses import dataclass, field
import hashlib

from src.anime.history import integer


class ReleaseNotReady(ValueError):
    reason_code = "RELEASE_UNRESOLVED"
    def __init__(self, reason_code="RELEASE_UNRESOLVED"):
        from src.anime.reasons import get_reason
        reason=get_reason(reason_code)
        if reason is None or not reason_code.startswith('RELEASE_'):
            raise ValueError('registered release reason required')
        self.reason_code=reason_code
        super().__init__(reason.message)


class ReleaseConflict(ValueError):
    reason_code = "RELEASE_CONFLICT"
    def __init__(self):
        super().__init__("Release episode information conflicts.")


@dataclass(frozen=True)
class ReleaseInput:
    event_id: int
    identity_result_id: int
    anidb_id: int
    tvdb_series_id: int
    raw_body: bytes = field(repr=False)

    def __post_init__(self):
        for value in (self.event_id,self.identity_result_id,self.anidb_id,self.tvdb_series_id):
            integer(value,positive=True)
        if type(self.raw_body) is not bytes or not 1<=len(self.raw_body)<=65536:
            raise ReleaseNotReady()

    @property
    def release_sha256(self):
        return hashlib.sha256(self.raw_body).hexdigest()


@dataclass(frozen=True)
class VerifiedRelease:
    source: ReleaseInput = field(repr=False)
    anidb_episode_number: int
    anidb_episode_id: int
    tvdb_season: int | None = None
    tvdb_episode: int | None = None
    tvdb_episode_id: int | None = None

    def __post_init__(self):
        if type(self.source) is not ReleaseInput: raise ValueError("release source required")
        for value in (self.anidb_episode_number,self.anidb_episode_id):
            integer(value,positive=True)
        if self.tvdb_season is not None:
            integer(self.tvdb_season)
        if self.tvdb_episode is not None:
            integer(self.tvdb_episode,positive=True)
        if self.tvdb_episode_id is not None:
            integer(self.tvdb_episode_id,positive=True)
