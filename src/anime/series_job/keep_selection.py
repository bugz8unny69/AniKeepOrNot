"""What Keep imports into Sonarr: one staged episode with both identities (was keep_selection.py)."""
from dataclasses import dataclass, field
import os
from pathlib import PurePosixPath
import stat

from src.anime.acquisition.cleanup import directory, parent
from src.anime.checks import here
from src.anime.history import integer
from src.anime.sonarr import KeepReview


class KeepSelectionInvalid(ValueError):
    reason_code = 'KEEP_IMPORT_REVIEW'
    def __init__(self, message=None, *, failed_check=None, failed_path=None):
        super().__init__(message or 'The staged episode could not be matched to its verified TVDB placement.')
        self.failed_check = failed_check
        self.failed_path = failed_path


@dataclass(frozen=True)
class KeepSelection:
    anidb_id: int
    anidb_episode_id: int
    tvdb_series_id: int
    tvdb_episode_id: int
    tvdb_season: int
    tvdb_episode: int
    preparation_plan_id: int
    staging_root: str = field(repr=False)
    media_relative_path: str = field(repr=False)

    def __post_init__(self):
        for name in ('anidb_id','anidb_episode_id','tvdb_series_id','tvdb_episode_id','tvdb_episode','preparation_plan_id'):
            integer(getattr(self,name),positive=True)
        integer(self.tvdb_season)
        from src.anime.acquisition.metadata import safe_path
        safe_path(self.media_relative_path)
        if not PurePosixPath(self.staging_root).is_absolute():raise KeepSelectionInvalid()

    @property
    def media_path(self):return str(PurePosixPath(self.staging_root)/self.media_relative_path)

    def match_sonarr_episode(self,series,episodes):
        """TVDB mapping is authoritative; titles/AniDB/Shoko do not pick a series."""
        from src.anime.episode_identity import EpisodeIdentityInvalid, VerifiedEpisode
        from src.anime.sonarr_episode import resolve_episode
        try:
            verified=VerifiedEpisode(self.tvdb_series_id,self.tvdb_episode_id,self.tvdb_season,self.tvdb_episode)
            return resolve_episode(verified,series,episodes)
        except EpisodeIdentityInvalid:  # Also covers SonarrEpisodeMismatch.
            raise KeepSelectionInvalid() from None


def check_source(data):
    selection = KeepSelection(**data['selection'])
    staged_abs = f"{selection.staging_root.rstrip('/')}/{selection.media_relative_path.lstrip('/')}"
    with directory(selection.staging_root) as root:
        with parent(root, selection.media_relative_path) as (folder, name):
            try:
                info = os.stat(name, dir_fd=folder, follow_symlinks=False)
            except FileNotFoundError:
                raise KeepReview(failed_check=here('STAGED_FILE_MISSING'),
                                 failed_path=staged_abs) from None
            if not stat.S_ISREG(info.st_mode) or info.st_size != data['source_file'][2]:
                raise KeepReview(failed_check=here('STAGED_FILE_CORRUPT'),
                                 failed_path=staged_abs)


def monitor_mode(highest,options):
    """Sonarr's addOptions.monitor for a new series (work item P1 1.4)."""
    threshold=options.long_running_threshold
    if threshold==0 or options.backfill_long_running or highest is None or highest<threshold:return 'all'
    return 'future'
