"""Immutable acquisition preparation inputs. Not download authorization."""
from dataclasses import dataclass, field
from src.anime.acquisition.settings import SizeLimits
from src.anime.acquisition.metadata import Manifest, ObservedInventory, infohash
from src.anime.history import integer


@dataclass(frozen=True)
class AcquisitionInput:
    reservation_id: int
    reservation_job_id: int
    evaluation_job_id: int
    event_id: int
    info_hash: str
    release_sha256: str
    anidb_episode_number: int
    tvdb_season: int
    tvdb_episode: int
    title: str = field(repr=False)
    def __post_init__(self):
        for name in ('reservation_id','reservation_job_id','evaluation_job_id','event_id','anidb_episode_number','tvdb_episode'):
            integer(getattr(self,name),positive=True)
        integer(self.tvdb_season)
        if infohash(self.info_hash)!=self.info_hash:raise ValueError('noncanonical torrent identity')


@dataclass(frozen=True)
class AcquisitionProposal:
    source: AcquisitionInput
    manifest: Manifest | ObservedInventory = field(repr=False)
    configuration_revision: int
    limits: SizeLimits = SizeLimits()
    preparation_job_id: int | None = None
    def __post_init__(self):
        if type(self.source) is not AcquisitionInput or type(self.manifest) not in (Manifest, ObservedInventory) or type(self.limits) is not SizeLimits:
            raise ValueError('typed acquisition facts required')
        integer(self.configuration_revision,positive=True)
        if self.preparation_job_id is not None:integer(self.preparation_job_id,positive=True)
        if self.source.info_hash!=self.manifest.info_hash:raise ValueError('torrent identity mismatch')
