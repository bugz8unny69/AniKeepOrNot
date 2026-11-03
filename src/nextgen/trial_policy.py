"""Bounded trial selection intents. Following count includes the trigger.

Single-episode trials select AniDB episode numbers (anidb_episodes); season
packs keep their TVDB-season window (episodes).
"""
from dataclasses import dataclass

from src.nextgen.history import integer, MAX_INTEGER


class TrialConflict(ValueError):
    reason_code="TRIAL_PLAN_CONFLICT"
    def __init__(self):super().__init__("The trial plan needs review.")


class InvalidEpisodeCounts(ValueError):
    reason_code="TRIAL_COUNTS_INVALID"
    def __init__(self):
        super().__init__("Set backfill or episodes starting with the received episode to at least 1.")


@dataclass(frozen=True)
class Window:
    backfill_count: int = 6
    following_count: int = 1

    def __post_init__(self):
        for value in (self.backfill_count,self.following_count):
            if type(value) is not int or not 0<=value<=1000: raise ValueError("invalid trial episode count")

        if self.backfill_count == 0 and self.following_count == 0:
            raise InvalidEpisodeCounts()

    def episodes(self, anchor):
        integer(anchor,positive=True)
        if self.following_count and anchor>MAX_INTEGER-self.following_count+1:
            raise TrialConflict()
        return tuple(sorted(set(range(1,self.backfill_count+1)) |
                            set(range(anchor,anchor+self.following_count))))

    def anidb_episodes(self, anidb_episode_number):
        """Single-episode selection window in AniDB episode numbers.

        Backfill is 1..N while the incoming episode is within the first N,
        otherwise the N episodes just before it. Continue is A..A+F-1.
        Season packs keep episodes() above; this never resets at a TVDB season.
        """
        anchor=anidb_episode_number
        integer(anchor,positive=True)
        if self.following_count and anchor>MAX_INTEGER-self.following_count+1:
            raise TrialConflict()
        n=self.backfill_count
        backfill=range(1,n+1) if anchor<=n else range(anchor-n,anchor)
        return tuple(sorted(set(backfill)|set(range(anchor,anchor+self.following_count))))


@dataclass(frozen=True)
class TrialSource:
    verification_id: int
    verification_job_id: int
    event_id: int
    anidb_id: int
    tvdb_series_id: int
    tvdb_season: int | None = None
    tvdb_episode: int | None = None
    anidb_episode_number: int = 1

    def __post_init__(self):
        for value in (self.verification_id,self.verification_job_id,self.event_id,
                      self.anidb_id,self.tvdb_series_id,
                      self.anidb_episode_number):integer(value,positive=True)
        if self.tvdb_season is not None:
            integer(self.tvdb_season)
        if self.tvdb_episode is not None:
            integer(self.tvdb_episode,positive=True)


@dataclass(frozen=True)
class TrialProposal:
    source: TrialSource
    window: Window
    configuration_revision: int

    def __post_init__(self):
        if type(self.source) is not TrialSource or type(self.window) is not Window: raise ValueError("invalid trial proposal")
        integer(self.configuration_revision,positive=True)
