"""Exact-episode reservation facts; no torrent ownership or external authority."""
from dataclasses import dataclass
from src.nextgen.history import integer


class ReservationConflict(ValueError):
    reason_code = 'EPISODE_RESERVATION_CONFLICT'
    def __init__(self):
        super().__init__('Episode reservation details need review.')


class EpisodeStaged(ValueError):
    """Exact verified episode has already been published into staging."""


class EpisodeReserved(ValueError):
    reason_code = 'EPISODE_RESERVED'
    def __init__(self, message=None, *, owner_job_id=None, operation_id=None, failed_check=None, failed_path=None):
        super().__init__(message or 'Another job has reserved this episode.')
        self.owner_job_id = owner_job_id
        self.operation_id = operation_id
        self.failed_check = failed_check
        self.failed_path = failed_path


@dataclass(frozen=True)
class ReservationInput:
    evaluation_job_id: int
    event_id: int
    verification_id: int
    plan_id: int
    anidb_id: int
    anidb_episode_id: int
    tvdb_series_id: int
    tvdb_season: int | None = None
    tvdb_episode: int | None = None
    tvdb_episode_id: int | None = None
    selected: bool = False

    def __post_init__(self):
        for name in ('evaluation_job_id','event_id','verification_id','plan_id','anidb_id',
                     'anidb_episode_id','tvdb_series_id'):
            integer(getattr(self,name),positive=True)
        if self.tvdb_season is not None:
            integer(self.tvdb_season)
        if self.tvdb_episode is not None:
            integer(self.tvdb_episode,positive=True)
        if self.tvdb_episode_id is not None:
            integer(self.tvdb_episode_id,positive=True)
        if type(self.selected) is not bool:raise ValueError('invalid episode selection')
