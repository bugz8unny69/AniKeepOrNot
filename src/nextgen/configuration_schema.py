"""Pure, versioned configuration validation/conversion and operation snapshots.

Configuration version, publication revision, SQLite version and AnimeSeries
version are independent. No disk, network, logging or legacy imports here.
"""
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import MappingProxyType
import copy
import ipaddress
import json
import re

from src.nextgen.settings import COMPONENTS, Settings, parse_settings

CONFIGURATION_VERSION = 14
MAX_REVISION = (1 << 63) - 1
MAX_BYTES = 65536


class ConfigurationError(ValueError):
    reason_code = "CONFIGURATION_INVALID"

    def __init__(self):
        super().__init__("Configuration is invalid.")


class ConfigurationEpisodeSelectionError(ConfigurationError):
    reason_code = "USER_EVALUATION_COUNTS_INVALID"

    def __init__(self):
        ValueError.__init__(self, "Set backfill or episodes starting with the received episode to at least 1.")


class ConfigurationConflict(ConfigurationError):
    reason_code = "CONFIGURATION_CONFLICT"

    def __init__(self):
        ValueError.__init__(self, "Configuration changed; refresh before applying changes.")


class ConfigurationMigrationRequired(ConfigurationError):
    reason_code = "CONFIGURATION_MIGRATION_REQUIRED"

    def __init__(self):
        ValueError.__init__(self, "Configuration requires an explicit version upgrade.")


class ConfigurationIOError(OSError):
    reason_code = "CONFIGURATION_STORAGE_FAILED"

    def __init__(self):
        super().__init__("Configuration storage could not be accessed safely.")


@dataclass(frozen=True)
class FieldDefinition:
    default: object
    kind: str
    apply_mode: str
    restart_components: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    label: str = ""
    help_text: str = ""


# Register new fields with their actual component; do not expose speculative
# provider/acquisition controls before those components define their semantics.
FIELDS = MappingProxyType({
    "anime.title_language": FieldDefinition("english", "title_language", "next_operation",
        label="Anime title language", help_text="English or Japanese (Romaji). Changes display names only."),
    "identity.match_threshold_pct": FieldDefinition(90, "integer", "next_operation", minimum=50, maximum=100,
        label="AniDB match threshold (%)", help_text="Configurable similarity percentage floor for automatic AniDB candidate matching (50–100). Default is 90%."),
    "shoko.base_url": FieldDefinition("", "download_endpoint", "next_operation", label="Shoko address", help_text="Optional Shoko v5 API v3 metadata lookup after Keep. Blank disables it. Does not move, rename, hash or register files."),
    "shoko.timeout_seconds": FieldDefinition(30, "integer", "next_operation", minimum=1, maximum=60, label="Shoko timeout"),
    "jellyfin.base_url": FieldDefinition("", "download_endpoint", "next_operation", label="Jellyfin address", help_text="HTTP(S) address without a subpath. Blank disables automatic library refresh. Configure both staging and permanent folders in Jellyfin first."),
    "jellyfin.timeout_seconds": FieldDefinition(30, "integer", "next_operation", minimum=1, maximum=60, label="Jellyfin timeout"),
    "prowlarr.base_url": FieldDefinition("", "download_endpoint", "next_operation", label="Prowlarr address", help_text="HTTP(S) server address for Prowlarr, used to search torrents for backfill and continue episodes. Blank disables the search."),
    "prowlarr.timeout_seconds": FieldDefinition(60, "integer", "next_operation", minimum=1, maximum=300, label="Prowlarr search timeout", help_text="Maximum seconds for one Prowlarr search. A timeout stops the search immediately; retry it from the job."),
    "prowlarr.max_resolution": FieldDefinition(1080, "resolution", "next_operation", label="Maximum resolution", help_text="Upper limit for backfill torrents, read from the release title. 1080 keeps 720p and 1080p; 2160 also permits 4K. Titles without a resolution are skipped."),
    "prowlarr.min_seeders": FieldDefinition(5, "integer", "next_operation", minimum=0, maximum=1000000, label="Minimum seeders", help_text="Releases with fewer seeders, or no seeder count, are skipped."),
    "prowlarr.rank_by": FieldDefinition("resolution", "ranking", "next_operation", label="Ranking", help_text="Pick the best release by resolution then seeders, or seeders then resolution."),
    "sonarr.base_url": FieldDefinition("", "download_endpoint", "next_operation", label="Sonarr address", help_text="HTTP(S) server address for Sonarr v4. Blank turns Keep off."),
    "sonarr.timeout_seconds": FieldDefinition(30, "integer", "next_operation", minimum=1, maximum=60, label="Sonarr timeout"),
    "sonarr.quality_profile_id": FieldDefinition(0, "integer", "next_operation", minimum=0, maximum=2147483647, label="Sonarr quality profile ID", help_text="Required for new series. Zero means unconfigured."),
    "sonarr.staging_root": FieldDefinition("", "directory", "next_operation", label="Staging folder as seen by Sonarr", help_text="Blank uses the recorded local staging root."),
    "sonarr.library_root": FieldDefinition("", "directory", "next_operation", label="Permanent folder as seen by Sonarr", help_text="Blank uses the local Keep folder. Local and Sonarr folders must refer to the same storage."),
    "qbittorrent.base_url": FieldDefinition("", "download_endpoint", "next_operation",
        label="qBittorrent address", help_text="HTTP or HTTPS server address without credentials, query or subpath. Required before download work."),
    "qbittorrent.category": FieldDefinition("anime-discovery", "download_category", "next_operation",
        label="Download category", help_text="Category label for newly submitted torrents. A label is not proof of ownership."),
    "qbittorrent.timeout_seconds": FieldDefinition(30, "integer", "next_operation", minimum=1, maximum=60,
        label="Download client timeout", help_text="Maximum seconds for a client request."),
    "qbittorrent.metadata_poll_seconds": FieldDefinition(5, "integer", "next_operation", minimum=1, maximum=10,
        label="Metadata poll interval (seconds)", help_text="Seconds between metadata status checks for qBittorrent 4.5–5.1. Higher values make fewer API calls."),
    "qbittorrent.metadata_poll_attempts": FieldDefinition(24, "integer", "next_operation", minimum=1, maximum=24,
        label="Metadata checks per window", help_text="Maximum metadata status checks before the owned magnet is stopped. The ten-minute total admission limit is unchanged."),
    "acquisition.max_episode_mb": FieldDefinition(600, "integer", "next_operation", minimum=1, maximum=1000000,
        label="Maximum video size (MB)", help_text="Hard limit for each selected video. Decimal MB. New selection-plan budgets only."),
    "acquisition.audio_check": FieldDefinition("japanese_english", "audio_check", "next_operation",
        label="Audio check", help_text="off, japanese or japanese_english. Which audio tracks a downloaded video must have (ffprobe, extra tracks are fine). japanese_english also tries Dual/Multi Audio releases first and searches Prowlarr instead of downloading an untagged autobrr release."),
    "acquisition.max_plan_mb": FieldDefinition(3000, "integer", "next_operation", minimum=1, maximum=1000000,
        label="Maximum selection-plan size (MB)", help_text="Combined hard limit for prepared selected videos, counting each episode once. Decimal MB. New budgets only."),
    "storage.download_root": FieldDefinition("", "directory", "next_operation",
        label="Download working folder", help_text="Absolute client working folder, separate from published video folders. Required before submission."),
    "trial.backfill_count": FieldDefinition(6, "integer", "next_operation", minimum=0, maximum=1000,
        label="Backfill first episodes", help_text="First X episodes of the verified season. Zero disables backfill; the other count must then be at least 1. New selection plans only."),
    "trial.following_count": FieldDefinition(1, "integer", "next_operation", minimum=0, maximum=1000,
        label="Episodes starting with the received episode", help_text="Includes the triggering episode: 1 keeps only that episode; 2 includes the next. Zero disables this window; backfill must then be at least 1. New selection plans only."),
    "anidb.client_name": FieldDefinition("", "client_name", "next_operation",
        label="AniDB client name", help_text="Registered AniDB HTTP client name. Required before identity work can start."),
    "anidb.client_version": FieldDefinition(1, "integer", "next_operation", minimum=1, maximum=999999,
        label="AniDB client version", help_text="Version registered for the AniDB HTTP client."),
    "providers.timeout_seconds": FieldDefinition(30, "integer", "next_operation", minimum=1, maximum=60,
        label="Provider request timeout", help_text="Maximum time in seconds for one provider request."),
    "logging.level": FieldDefinition("INFO", "level", "next_operation"),
    "logging.components": FieldDefinition(MappingProxyType({}), "levels", "next_operation"),
    "api.host": FieldDefinition("127.0.0.1", "host", "restart", ("api",)),
    "api.port": FieldDefinition(8765, "integer", "restart", ("api",), 1, 65535),
    "worker.concurrency": FieldDefinition(4, "integer", "restart", ("worker",), 1, 32),
    "storage.trial_root": FieldDefinition("", "directory", "next_operation",
        label="Staging folder", help_text="Absolute destination for verified videos staged for viewing before Keep or Reject. Leave blank until configured. Existing files are not moved."),
    "storage.keep_root": FieldDefinition("", "directory", "next_operation",
        label="Keep video folder", help_text="Absolute permanent destination for approved Keep videos. Must agree with the library import target when Sonarr is used."),
    "storage.database_path": FieldDefinition(
        "/var/lib/anime-discovery/nextgen.sqlite", "path", "restart", tuple(sorted(COMPONENTS))),
})

# The obsolete HTTP metadata-source fields exist only to validate published
# v7-v11 documents during *explicit* migrations, never in live settings.
V13_FIELDS = MappingProxyType({key: spec for key, spec in FIELDS.items()
    if key != "identity.match_threshold_pct"})
V12_FIELDS = MappingProxyType({key: spec for key, spec in V13_FIELDS.items()
    if key != "anime.title_language"})
V11_FIELDS = MappingProxyType({**V12_FIELDS,
    "torrent_metadata.base_url": FieldDefinition("", "metadata_source", "next_operation"),
    "torrent_metadata.timeout_seconds": FieldDefinition(30, "integer", "next_operation", minimum=1, maximum=60),
})


def revision(value):
    if type(value) is not int or not 1 <= value <= MAX_REVISION:
        raise ConfigurationError()
    return value


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    return value


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    return value


def defaults(*, fields=FIELDS):
    result = {}
    for path, spec in fields.items():
        section, name = path.split(".")
        result.setdefault(section, {})[name] = _plain(spec.default)
    return result


def field_definitions():
    """Detached JSON-compatible metadata for the future authenticated API."""
    return {path: {"default": _plain(spec.default), "type": spec.kind,
                   "apply_mode": spec.apply_mode, "restart_components": list(spec.restart_components),
                   "minimum": spec.minimum, "maximum": spec.maximum,
                   "label": spec.label or path, "help": spec.help_text}
            for path, spec in FIELDS.items()}


def _host(value):
    if (type(value) is not str or not 1 <= len(value) <= 253 or not value.isascii()
            or "%" in value or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                   for label in value.split("."))


def validate_values(value, *, fields=FIELDS):
    if type(value) is not dict:
        raise ConfigurationError()
    result = defaults(fields=fields)
    allowed = fields
    for section, fields in value.items():
        if type(section) is not str or section not in result or type(fields) is not dict:
            raise ConfigurationError()
        for key, item in fields.items():
            if type(key) is not str or section + "." + key not in allowed:
                raise ConfigurationError()
            spec = allowed[section + "." + key]
            if spec.kind == "integer":
                if type(item) is not int or not spec.minimum <= item <= spec.maximum:
                    raise ConfigurationError()
            elif spec.kind == "resolution":
                if type(item) is not int or item not in (720, 1080, 2160):
                    raise ConfigurationError()
            elif spec.kind == "audio_check":
                if item is None or item == "":
                    item = "japanese"
                elif type(item) is not str or item not in ("off", "japanese", "japanese_english"):
                    raise ConfigurationError()
            elif spec.kind == "title_language":
                if type(item) is not str or item not in ("english", "japanese"):
                    raise ConfigurationError()
            elif spec.kind == "ranking":
                if type(item) is not str or item not in ("resolution", "seeders"):
                    raise ConfigurationError()
            elif spec.kind == "metadata_source":
                if type(item) is not str:raise ConfigurationError()
                if item:
                    from src.nextgen.acquisition.metainfo_source import source_url
                    try:source_url(item)
                    except ValueError:raise ConfigurationError() from None
            elif spec.kind == "download_endpoint":
                if type(item) is not str:raise ConfigurationError()
                if item:
                    from src.nextgen.acquisition.settings import endpoint
                    try:endpoint(item)
                    except ValueError:raise ConfigurationError() from None
            elif spec.kind == "download_category":
                if type(item) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,64}',item):raise ConfigurationError()
            elif spec.kind == "client_name":
                if type(item) is not str or (item and not re.fullmatch(r"[A-Za-z0-9_-]{3,32}",item)):
                    raise ConfigurationError()
            elif spec.kind == "host":
                if not _host(item):
                    raise ConfigurationError()
            elif spec.kind == "path":
                if (type(item) is not str or len(item) > 4096
                        or not PurePosixPath(item).is_absolute()
                        or str(PurePosixPath(item)) == "/"
                        or any(ord(char) < 32 or ord(char) == 127 for char in item)):
                    raise ConfigurationError()
            elif spec.kind == "directory":
                if type(item) is not str:
                    raise ConfigurationError()
                if item and (not PurePosixPath(item).is_absolute() or item.startswith("//")
                        or item == "/" or str(PurePosixPath(item)) != item
                        or any(part in (".", "..") for part in item.split("/"))
                        or any(ord(char) < 32 or ord(char) == 127 for char in item)):
                    raise ConfigurationError()
            elif spec.kind == "levels":
                if type(item) is not dict:
                    raise ConfigurationError()
            result[section][key] = copy.deepcopy(item)
    try:
        # Reuse the existing strict component/level validator.
        parse_settings({"logging": result["logging"]})
    except (ValueError, TypeError):
        raise ConfigurationError() from None
    roots = [PurePosixPath(result["storage"][key]) for key in ("trial_root", "keep_root", "download_root")
             if result["storage"][key]]
    if any(a.is_relative_to(b) or b.is_relative_to(a) for i,a in enumerate(roots) for b in roots[i+1:]):
        raise ConfigurationError()
    database = PurePosixPath(result["storage"]["database_path"])
    if any(database.is_relative_to(root) for root in roots):
        raise ConfigurationError()
    if result["trial"]["backfill_count"] == 0 and result["trial"]["following_count"] == 0:
        raise ConfigurationEpisodeSelectionError()
    return result


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError()
        result[key] = value
    return result


def _invalid_constant(value):
    raise ConfigurationError()


def decode(raw):
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        raise ConfigurationError()
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ConfigurationError() from None


@dataclass(frozen=True)
class ConfigurationSnapshot:
    revision: int
    values: Mapping = field(repr=False)
    schema_version: int = CONFIGURATION_VERSION

    def __post_init__(self):
        revision(self.revision)
        if type(self.schema_version) is not int or self.schema_version != CONFIGURATION_VERSION:
            raise ConfigurationError()
        # Detached, deeply immutable validated values even for direct construction.
        object.__setattr__(self, "values", _freeze(validate_values(_plain(self.values))))

    def value(self, path):
        if type(path) is not str or path not in FIELDS:
            raise ConfigurationError()
        section, key = path.split(".")
        val = self.values[section][key]
        if path == "acquisition.audio_check" and (val is None or val == ""):
            return "japanese"
        return val

    def to_document(self):
        return {"schema_version": self.schema_version, "revision": self.revision,
                "settings": _plain(self.values)}

    def logging_settings(self) -> Settings:
        return parse_settings({"logging": _plain(self.values["logging"])})


def document_snapshot(document):
    if (type(document) is not dict
            or set(document) != {"schema_version", "revision", "settings"}):
        raise ConfigurationError()
    return ConfigurationSnapshot(document["revision"], document["settings"], document["schema_version"])


def source_version(document):
    if type(document) is not dict:
        raise ConfigurationError()
    if "schema_version" not in document:
        # v0 means only the previously supported unversioned NextGen logging
        # object. This is deliberately NOT a converter for legacy production.
        if set(document) - {"logging"}:
            raise ConfigurationError()
        try:
            _v1_values(document)
        except (ValueError, TypeError):
            raise ConfigurationError() from None
        return 0
    version = document["schema_version"]
    if type(version) is not int or not 1 <= version <= CONFIGURATION_VERSION:
        raise ConfigurationError()
    return version


def _v1_values(values):
    """Validate the published v1 shape without accepting current names there.

    Round-trip through current validation so all unchanged bounds stay strict,
    then restore historical names. The original v0→v1 output remains v1-shaped.
    """
    if type(values) is not dict or "worker" in values:
        raise ConfigurationError()
    current = copy.deepcopy(values)
    if "coordinator" in current:
        current["worker"] = current.pop("coordinator")
    logging_values = current.get("logging", {})
    if type(logging_values) is not dict:
        raise ConfigurationError()
    components = logging_values.get("components", {})
    if type(components) is not dict or "worker" in components:
        raise ConfigurationError()
    if "coordinator" in components:
        components["worker"] = components.pop("coordinator")
    historical = _v2_values(current)
    historical["coordinator"] = historical.pop("worker")
    levels = historical["logging"]["components"]
    if "worker" in levels:
        levels["coordinator"] = levels.pop("worker")
    return historical


def _v0_to_v1(document):
    return {"schema_version": 1, "revision": 1, "settings": _v1_values(document)}


def _v1_to_v2(document):
    if (type(document) is not dict
            or set(document) != {"schema_version", "revision", "settings"}
            or document["schema_version"] != 1):
        raise ConfigurationError()
    current_revision = revision(document["revision"])
    if current_revision == MAX_REVISION:
        raise ConfigurationError()
    values = _v1_values(document["settings"])
    values["worker"] = values.pop("coordinator")
    levels = values["logging"]["components"]
    if "coordinator" in levels:
        levels["worker"] = levels.pop("coordinator")
    return {"schema_version": 2, "revision": current_revision + 1, "settings": values}


def _v2_values(values):
    """Freeze published v2 keys/defaults; additions belong only to v3."""
    if type(values) is not dict:
        raise ConfigurationError()
    storage = values.get("storage", {})
    if type(storage) is not dict or set(storage) & {"trial_root", "keep_root"}:
        raise ConfigurationError()
    historical = _v3_values(values)
    del historical["storage"]["trial_root"]
    del historical["storage"]["keep_root"]
    return historical


def _v2_to_v3(document):
    if (type(document) is not dict
            or set(document) != {"schema_version", "revision", "settings"}
            or document["schema_version"] != 2):
        raise ConfigurationError()
    current_revision = revision(document["revision"])
    if current_revision == MAX_REVISION:
        raise ConfigurationError()
    return {"schema_version": 3, "revision": current_revision + 1,
            "settings": _v3_values(_v2_values(document["settings"]))}


def _v3_values(values):
    """Validate published v3 without adding or accepting v4 provider sections."""
    if type(values) is not dict or set(values) & {"anidb", "providers"}:
        raise ConfigurationError()
    historical = _v4_values(values)
    del historical["anidb"]
    del historical["providers"]
    return historical


def _v3_to_v4(document):
    if (type(document) is not dict or set(document)!={"schema_version","revision","settings"}
            or document["schema_version"]!=3):
        raise ConfigurationError()
    current = revision(document["revision"])
    if current == MAX_REVISION: raise ConfigurationError()
    return {"schema_version":4,"revision":current+1,"settings":_v4_values(_v3_values(document["settings"]))}


def _v4_values(values):
    """Keep published v4 output free of new trial settings."""
    if type(values) is not dict or "trial" in values: raise ConfigurationError()
    historical=_v5_values(values)
    del historical["trial"]
    return historical


def _v4_to_v5(document):
    if (type(document) is not dict or set(document)!={"schema_version","revision","settings"}
            or document["schema_version"]!=4): raise ConfigurationError()
    current=revision(document["revision"])
    if current==MAX_REVISION: raise ConfigurationError()
    return {"schema_version":5,"revision":current+1,"settings":_v5_values(_v4_values(document["settings"]))}


def _v5_values(values):
    """Published v5 has no client/size settings or download_root."""
    if type(values) is not dict or set(values) & {'qbittorrent','acquisition'}:raise ConfigurationError()
    storage=values.get('storage',{})
    if type(storage) is not dict or 'download_root' in storage:raise ConfigurationError()
    historical=_v6_values(values)
    del historical['qbittorrent']
    del historical['acquisition']
    del historical['storage']['download_root']
    return historical


def _v5_to_v6(document):
    if (type(document) is not dict or set(document)!={'schema_version','revision','settings'}
            or document['schema_version']!=5):raise ConfigurationError()
    current=revision(document['revision'])
    if current==MAX_REVISION:raise ConfigurationError()
    return {'schema_version':6,'revision':current+1,'settings':_v6_values(_v5_values(document['settings']))}


def _v6_values(values):
    """Keep published v6 shapes free of metadata-source fields."""
    if type(values) is not dict or 'torrent_metadata' in values:raise ConfigurationError()
    historical=_v7_values(values)
    del historical['torrent_metadata']
    return historical


def _v6_to_v7(document):
    if (type(document) is not dict or set(document)!={'schema_version','revision','settings'}
            or document['schema_version']!=6):raise ConfigurationError()
    current=revision(document['revision'])
    if current==MAX_REVISION:raise ConfigurationError()
    return {'schema_version':7,'revision':current+1,'settings':_v7_values(_v6_values(document['settings']))}


def _v7_values(values):
    if type(values) is not dict or 'sonarr' in values:raise ConfigurationError()
    historical=_v8_values(values)
    del historical['sonarr']
    return historical


def _v7_to_v8(document):
    if (type(document) is not dict or set(document)!={'schema_version','revision','settings'} or document['schema_version']!=7):raise ConfigurationError()
    current=revision(document['revision'])
    if current==MAX_REVISION:raise ConfigurationError()
    return {'schema_version':8,'revision':current+1,'settings':_v8_values(_v7_values(document['settings']))}


def _v8_values(values):
    if type(values) is not dict or 'jellyfin' in values:raise ConfigurationError()
    historical=_v9_values(values)
    del historical['jellyfin']
    return historical


def _v8_to_v9(document):
    if (type(document) is not dict or set(document)!={'schema_version','revision','settings'} or document['schema_version']!=8):raise ConfigurationError()
    current=revision(document['revision'])
    if current==MAX_REVISION:raise ConfigurationError()
    return {'schema_version':9,'revision':current+1,'settings':_v9_values(_v8_values(document['settings']))}


def _v9_values(values):
    if type(values) is not dict or 'shoko' in values:raise ConfigurationError()
    historical=_v10_values(values)
    del historical['shoko']
    return historical


def _v9_to_v10(document):
    if (type(document) is not dict or set(document)!={'schema_version','revision','settings'} or document['schema_version']!=9):raise ConfigurationError()
    current=revision(document['revision'])
    if current==MAX_REVISION:raise ConfigurationError()
    return {'schema_version':10,'revision':current+1,'settings':_v10_values(_v9_values(document['settings']))}


V10_DISCOVERY = (('discovery_max_resolution','max_resolution'),('discovery_min_seeders','min_seeders'),
                 ('discovery_rank_by','rank_by'))


def _v10_values(values):
    """Published v10: search preferences under sonarr.discovery_*, no prowlarr section."""
    if type(values) is not dict or 'prowlarr' in values:raise ConfigurationError()
    current=copy.deepcopy(values)
    sonarr=current.get('sonarr',{})
    if type(sonarr) is not dict:raise ConfigurationError()
    moved={new:sonarr.pop(old) for old,new in V10_DISCOVERY if old in sonarr}
    if moved:current['prowlarr']=moved
    historical=validate_values(current,fields=V11_FIELDS)
    search=historical.pop('prowlarr')
    for old,new in V10_DISCOVERY:historical['sonarr'][old]=search[new]
    return historical


def _v10_to_v11(document):
    """Move the discovery preferences from sonarr.* to the new prowlarr section."""
    if (type(document) is not dict or set(document)!={'schema_version','revision','settings'} or document['schema_version']!=10):raise ConfigurationError()
    current=revision(document['revision'])
    if current==MAX_REVISION:raise ConfigurationError()
    values=_v10_values(document['settings'])
    values['prowlarr']={new:values['sonarr'].pop(old) for old,new in V10_DISCOVERY}
    return {'schema_version':11,'revision':current+1,'settings':validate_values(values,fields=V11_FIELDS)}


def _v11_to_v12(document):
    """Retire HTTP metainfo source while preserving other version-11 values."""
    if (type(document) is not dict or set(document)!={'schema_version','revision','settings'}
            or document['schema_version']!=11):raise ConfigurationError()
    current=revision(document['revision'])
    if current==MAX_REVISION:raise ConfigurationError()
    previous=validate_values(document['settings'],fields=V11_FIELDS)
    del previous['torrent_metadata']
    return {'schema_version':12,'revision':current+1,'settings':validate_values(previous, fields=V12_FIELDS)}


def _v12_to_v13(document):
    """Add display-only anime title preference, preserving previous configuration."""
    if (type(document) is not dict or set(document) != {'schema_version', 'revision', 'settings'}
            or document['schema_version'] != 12):
        raise ConfigurationError()
    current = revision(document['revision'])
    if current == MAX_REVISION:
        raise ConfigurationError()
    previous = validate_values(document['settings'], fields=V12_FIELDS)
    return {'schema_version': 13, 'revision': current+1, 'settings': validate_values(previous, fields=V13_FIELDS)}


def _v13_to_v14(document):
    """Add AniDB identity match threshold percentage, preserving previous configuration."""
    if (type(document) is not dict or set(document) != {'schema_version', 'revision', 'settings'}
            or document['schema_version'] != 13):
        raise ConfigurationError()
    current = revision(document['revision'])
    if current == MAX_REVISION:
        raise ConfigurationError()
    previous = validate_values(document['settings'], fields=V13_FIELDS)
    return {'schema_version': 14, 'revision': current+1, 'settings': validate_values(previous)}


# Preserve published version shapes and validate before dropping obsolete keys.
CONVERSIONS = MappingProxyType({0: _v0_to_v1, 1: _v1_to_v2, 2: _v2_to_v3, 3: _v3_to_v4, 4: _v4_to_v5, 5: _v5_to_v6, 6: _v6_to_v7, 7: _v7_to_v8, 8: _v8_to_v9, 9: _v9_to_v10, 10: _v10_to_v11, 11: _v11_to_v12, 12: _v12_to_v13, 13: _v13_to_v14})


def convert_document(document):
    version = source_version(document)
    result = copy.deepcopy(document)
    while version < CONFIGURATION_VERSION:
        converter = CONVERSIONS.get(version)
        if converter is None:
            raise ConfigurationMigrationRequired()
        result = converter(result)
        if type(result) is not dict or result.get("schema_version") != version + 1:
            raise ConfigurationError()
        version += 1
    return document_snapshot(result)


def load_current(raw):
    document = decode(raw)
    if source_version(document) != CONFIGURATION_VERSION:
        raise ConfigurationMigrationRequired()
    return document_snapshot(document)


def encode(snapshot):
    encoded = (json.dumps(snapshot.to_document(), sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise ConfigurationError()
    return encoded


def patch_snapshot(current, changes):
    if type(changes) is not dict or not changes:
        raise ConfigurationError()
    candidate = current.to_document()["settings"]
    for section, values in changes.items():
        if type(section) is not str or section not in candidate or type(values) is not dict:
            raise ConfigurationError()
        for key, value in values.items():
            if type(key) is not str or section + "." + key not in FIELDS:
                raise ConfigurationError()
            candidate[section][key] = copy.deepcopy(value)
    validated = validate_values(candidate)
    changed = tuple(path for path in FIELDS
                    if _plain(current.value(path)) != validated[path.split(".")[0]][path.split(".")[1]])
    if not changed:
        return current, ()
    if current.revision == MAX_REVISION:
        raise ConfigurationError()
    return ConfigurationSnapshot(current.revision + 1, validated), changed


@dataclass(frozen=True)
class OperationConfiguration:
    desired_revision: int
    startup_revision: int
    component: str
    values: Mapping = field(repr=False)
    pending_restart_fields: tuple[str, ...]

    def value(self, path):
        if type(path) is not str or path not in FIELDS:
            raise ConfigurationError()
        section, key = path.split(".")
        val = self.values[section][key]
        if path == "acquisition.audio_check" and (val is None or val == ""):
            return "japanese"
        return val

    def logging_settings(self):
        return parse_settings({"logging": _plain(self.values["logging"])})


def for_operation(latest, startup, *, component):
    """Fresh operational values, pinned startup values, no global reload signal."""
    if type(component) is not str or component not in COMPONENTS:
        raise ConfigurationError()
    if latest.revision < startup.revision:
        raise ConfigurationConflict()
    effective = latest.to_document()["settings"]
    pending = []
    for path, spec in FIELDS.items():
        if spec.apply_mode == "restart":
            section, key = path.split(".")
            effective[section][key] = _plain(startup.value(path))
            if component in spec.restart_components and latest.value(path) != startup.value(path):
                pending.append(path)
    # Desired values were validated together, but pinning startup-only fields
    # creates a different combination. Recheck cross-field constraints before
    # exposing it (for example media roots versus the still-active database).
    effective = validate_values(effective)
    return OperationConfiguration(latest.revision, startup.revision, component,
                                  _freeze(effective), tuple(pending))
