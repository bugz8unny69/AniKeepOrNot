"""Reuse staged output and both provider identities for Keep; no Shoko dependency."""
from dataclasses import dataclass, field
import json
from pathlib import PurePosixPath

from src.nextgen.history import integer
from src.nextgen.storage.cancellation import operation
from src.nextgen.storage.review_decisions import staged, require_allowed


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
        from src.nextgen.acquisition.metadata import safe_path
        safe_path(self.media_relative_path)
        if not PurePosixPath(self.staging_root).is_absolute():raise KeepSelectionInvalid()

    @property
    def media_path(self):return str(PurePosixPath(self.staging_root)/self.media_relative_path)

    def match_sonarr_episode(self,series,episodes):
        """TVDB mapping is authoritative; titles/AniDB/Shoko do not pick a series."""
        from src.nextgen.episode_identity import EpisodeIdentityInvalid, VerifiedEpisode
        from src.nextgen.sonarr_episode import resolve_episode
        try:
            verified=VerifiedEpisode(self.tvdb_series_id,self.tvdb_episode_id,self.tvdb_season,self.tvdb_episode)
            return resolve_episode(verified,series,episodes)
        except EpisodeIdentityInvalid:  # Also covers SonarrEpisodeMismatch.
            raise KeepSelectionInvalid() from None


def load_selection(db,target_job_id,*,resolver=None):
    """Read the existing staged manifest, rather than rebuilding names or NFOs."""
    op=operation(db,target_job_id)
    if op[1]!='STAGED':raise KeepSelectionInvalid()
    source=staged(db,op[0]);require_allowed(db,source[0])
    identity=db.execute('SELECT r.anidb_id,r.anidb_episode_id,r.tvdb_series_id,r.tvdb_episode_id,r.tvdb_season,r.tvdb_episode '
        'FROM acquisition_plans p JOIN episode_reservations r USING(reservation_id) WHERE p.plan_id=?',(source[1],)).fetchone()
    if identity is None:raise KeepSelectionInvalid()
    aid, eid, tid, tvdb_eid, season, ep = identity
    if tvdb_eid is None or season is None or ep is None:
        known = db.execute('SELECT tvdb_episode_id, tvdb_season, tvdb_episode FROM recognized_episodes '
                           'WHERE anidb_id=? AND anidb_episode_id=?', (aid, eid)).fetchone()
        if known is not None:
            tvdb_eid, season, ep = known
        elif resolver is not None:
            resolved = resolver(db, aid, eid, tid, target_job_id)
            if resolved is not None:
                tvdb_eid, season, ep = resolved
            else:
                raise KeepSelectionInvalid()
        else:
            raise KeepSelectionInvalid()
    identity = (aid, eid, tid, tvdb_eid, season, ep)
    manifest=json.loads(source[2]);receipts=json.loads(source[3])
    media=[entry for entry in manifest['entries'] if entry['role']=='media']
    proof=[entry for entry in receipts if entry['role']=='media']
    if len(media)!=1 or len(proof)!=1 or media[0]['path']!=proof[0]['path']:raise KeepSelectionInvalid()
    return KeepSelection(*identity,source[1],manifest['root'],media[0]['path'])
