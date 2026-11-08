"""Validated local download settings and size policy; no filesystem/HTTP calls."""
from dataclasses import dataclass, field
import re
import unicodedata
from urllib.parse import urlsplit

MB=1_000_000


class DownloadConfigurationError(ValueError):
    reason_code='DOWNLOAD_CONFIGURATION_INVALID'
    def __init__(self):super().__init__('Configure the download client and download folder before acquisition.')


class SizeLimitExceeded(ValueError):
    reason_code='ACQUISITION_SIZE_LIMIT'
    def __init__(self,facts=None,*,plan=False):
        # facts: video_mb, limit_mb and (for the trial total) plan_used_mb, shown to the user.
        self.facts=facts
        if plan:self.reason_code='ACQUISITION_PLAN_SIZE_LIMIT'
        super().__init__('The video is over the '+('trial total' if plan else 'episode')+' size limit.'
                         +('' if facts is None else ' '+str(facts)))


class SizeEvidenceMissing(ValueError):
    reason_code='ACQUISITION_SIZE_EVIDENCE_MISSING'
    def __init__(self):super().__init__('An existing acquisition plan needs its file size reviewed.')


def endpoint(value):
    if type(value) is not str or not value or len(value)>2048 or not value.isascii():raise DownloadConfigurationError()
    if any(ord(c)<=32 or ord(c)==127 for c in value) or '\\' in value:raise DownloadConfigurationError()
    try:
        parsed=urlsplit(value)
        if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.netloc.endswith(':') or parsed.query or parsed.fragment or parsed.path not in ('','/')
                or '?' in value or '#' in value or parsed.port is not None and not 1<=parsed.port<=65535):
            raise DownloadConfigurationError()
        host=parsed.hostname
        import ipaddress
        try:ipaddress.ip_address(host)
        except ValueError:
            if not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?',p) for p in host.split('.')):
                raise DownloadConfigurationError()
    except ValueError:raise DownloadConfigurationError() from None
    return value.rstrip('/')


@dataclass(frozen=True)
class DownloadCredentials:
    username: str = field(repr=False)
    password: str = field(repr=False)
    def __post_init__(self):
        for value,limit in ((self.username,256),(self.password,1024)):
            if (type(value) is not str or not 1<=len(value)<=limit
                    or any(unicodedata.category(c).startswith('C') for c in value)):
                raise DownloadConfigurationError()


@dataclass(frozen=True)
class DownloadOptions:
    base_url: str = field(repr=False)
    download_root: str = field(repr=False)
    category: str
    timeout_seconds: int
    def __post_init__(self):
        object.__setattr__(self,'base_url',endpoint(self.base_url))
        from pathlib import PurePosixPath
        p=self.download_root
        if (type(p) is not str or not p.startswith('/') or p.startswith('//') or p=='/'
                or len(p)>4096 or str(PurePosixPath(p))!=p or any(x in ('.','..') for x in p.split('/'))
                or any(ord(c)<32 or ord(c)==127 for c in p)):raise DownloadConfigurationError()
        if type(self.category) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,64}',self.category):raise DownloadConfigurationError()
        if type(self.timeout_seconds) is not int or not 1<=self.timeout_seconds<=60:raise DownloadConfigurationError()


@dataclass(frozen=True)
class SizeLimits:
    max_episode_mb: int = 600
    max_plan_mb: int = 3000
    def __post_init__(self):
        for value in (self.max_episode_mb,self.max_plan_mb):
            if type(value) is not int or not 1<=value<=1_000_000:raise DownloadConfigurationError()
    @property
    def episode_bytes(self):return self.max_episode_mb*MB
    @property
    def plan_bytes(self):return self.max_plan_mb*MB
    def admit(self,size,recorded_total=0):
        if type(size) is not int or size<=0 or type(recorded_total) is not int or recorded_total<0:
            raise SizeEvidenceMissing()
        video=-(-size//MB)
        if size>self.episode_bytes:
            raise SizeLimitExceeded({'video_mb':video,'limit_mb':self.max_episode_mb})
        if recorded_total+size>self.plan_bytes:
            raise SizeLimitExceeded({'video_mb':video,'plan_used_mb':-(-recorded_total//MB),
                                     'limit_mb':self.max_plan_mb},plan=True)


def download_options(configuration):
    return DownloadOptions(configuration.value('qbittorrent.base_url'),configuration.value('storage.download_root'),
                           configuration.value('qbittorrent.category'),configuration.value('qbittorrent.timeout_seconds'))


def size_limits(configuration):
    return SizeLimits(configuration.value('acquisition.max_episode_mb'),configuration.value('acquisition.max_plan_mb'))


def download_context(configuration,secrets):
    """Capture a fresh immutable local context at point of use; no login/probes."""
    return download_options(configuration),secrets.qbittorrent_credentials()
