"""AnimeSeries v2: a series identity schema, with optional episode coverage.

No delivery IDs, logging, jobs, acquisition, staging or evaluation state.
A series can be identified before episodes in a season pack are resolved.
Any populated episode still requires an exact AniDB episode ID. A failed schema
check is an individual identification result, not a process crash.
"""
from dataclasses import asdict, dataclass, field, fields, MISSING
import copy
import json
from typing import Optional

SCHEMA_VERSION = 2
FIELD_DOC_KEYS = frozenset({"meaning", "reason", "owner", "available", "origin"})


def documented(*, meaning: str, reason: str, owner: str, available: str, origin: str):
    return field(metadata=dict(meaning=meaning, reason=reason, owner=owner,
                               available=available, origin=origin))


def optional(*, meaning: str, reason: str, owner: str, available: str, origin: str):
    return field(default=None, metadata=dict(meaning=meaning, reason=reason,
                               owner=owner, available=available, origin=origin))


def positive(value, name: str):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} MUST be a positive integer")


def optional_text(value, name: str):
    if value is not None and (type(value) is not str or not value.strip()):
        raise ValueError(f"{name} MUST be nonblank text or null")


@dataclass(frozen=True)
class TVDBPlacement:
    """Optional per-episode TVDB placement, not required for identity completion."""
    season: int = documented(meaning="TVDB season number, including zero for specials",
        reason="Preserve known episode placement without guessing", owner="identity",
        available="when mapping identified", origin="TVDB cross-reference")
    episode: int = documented(meaning="TVDB episode number within the season",
        reason="Enable downstream operations requiring an exact placement", owner="identity",
        available="when mapping identified", origin="TVDB cross-reference")

    def __post_init__(self):
        if type(self.season) is not int or self.season < 0:
            raise ValueError("tvdb_placement.season MUST be a nonnegative integer")
        positive(self.episode, "tvdb_placement.episode")


@dataclass(frozen=True)
class Anime:
    """Identified anime, including its Sonarr-compatible TVDB series relationship."""
    anidb_id: int = documented(meaning="Identified AniDB anime ID",
        reason="Canonical anime identity, including distinct sequel entries",
        owner="identity", available="identity completion", origin="AniDB")
    title: str = documented(meaning="Identified anime title, not webhook release name",
        reason="Describe the series to every consumer", owner="identity",
        available="series identity completion", origin="anime metadata")
    tvdb_series_id: int = documented(meaning="Verified TVDB series ID for Sonarr",
        reason="Required series-level connection so the downstream Sonarr pipeline can operate",
        owner="identity", available="series identity completion", origin="TVDB cross-reference")
    english_title: Optional[str] = optional(meaning="English anime title",
        reason="Useful catalog and display data", owner="identity",
        available="when known", origin="anime metadata")
    original_title: Optional[str] = optional(meaning="Original-language anime title",
        reason="Preserve native identity information", owner="identity",
        available="when known", origin="anime metadata")
    type: Optional[str] = optional(meaning="Anime form, such as TV or OVA",
        reason="Describe the identified work", owner="identity",
        available="when known", origin="anime metadata")
    year: Optional[int] = optional(meaning="First release year",
        reason="Disambiguate similarly named anime", owner="identity",
        available="when known", origin="anime metadata")

    def __post_init__(self):
        positive(self.anidb_id, "anime.anidb_id")
        positive(self.tvdb_series_id, "anime.tvdb_series_id")
        if type(self.title) is not str or not self.title.strip():
            raise ValueError("anime.title MUST be nonblank identified anime title")
        for name in ("english_title", "original_title", "type"):
            optional_text(getattr(self, name), f"anime.{name}")
        if self.year is not None:
            positive(self.year, "anime.year")


@dataclass(frozen=True)
class Episode:
    """A single AniDB episode; numbering is descriptive, never a dedup key."""
    anidb_episode_id: int = documented(meaning="Identified AniDB episode ID",
        reason="Exact Shoko-compatible episode identity", owner="identity",
        available="identity completion", origin="AniDB")
    anidb_episode_number: Optional[int | str] = optional(
        meaning="Display numbering as recorded by AniDB",
        reason="Accommodate continuous numbering, sequels and nonstandard specials",
        owner="identity", available="when known", origin="AniDB")
    title: Optional[str] = optional(meaning="Identified episode title",
        reason="Enrich series metadata", owner="identity",
        available="when known", origin="episode metadata")
    type: Optional[str] = optional(meaning="Episode classification, e.g. REGULAR",
        reason="Distinguish regular episodes from specials", owner="identity",
        available="when known", origin="AniDB")
    tvdb_placement: Optional[TVDBPlacement] = optional(
        meaning="Optional known TVDB season and episode pair",
        reason="Preserve cross-provider placement without requiring episode-level TVDB data",
        owner="identity", available="only when mapped", origin="TVDB cross-reference")

    def __post_init__(self):
        positive(self.anidb_episode_id, "episodes[].anidb_episode_id")
        number = self.anidb_episode_number
        if number is not None:
            if type(number) is int and number < 0:
                raise ValueError("episodes[].anidb_episode_number MUST be nonnegative")
            if type(number) is str and not number.strip():
                raise ValueError("episodes[].anidb_episode_number MUST be nonblank")
            if type(number) not in (int, str):
                raise ValueError("episodes[].anidb_episode_number MUST be int, text or null")
        optional_text(self.title, "episodes[].title")
        optional_text(self.type, "episodes[].type")
        if self.tvdb_placement is not None and not isinstance(self.tvdb_placement, TVDBPlacement):
            raise ValueError("episodes[].tvdb_placement MUST be a valid mapping or null")


@dataclass(frozen=True)
class AnimeSeries:
    """A valid series identity; episode entries are present only when identified."""
    schema_version: int = documented(meaning="Version of the AnimeSeries schema",
        reason="Protect consumers from incompatible schema changes",
        owner="schema", available="always", origin="schema")
    anime: Anime = documented(meaning="Identified anime identity and metadata",
        reason="Share one verified series-level series description",
        owner="identity", available="identity completion", origin="identity result")
    episodes: tuple[Episode, ...] = field(default_factory=tuple, metadata=dict(
        meaning="Zero or more individually identified AniDB episodes",
        reason="Allow series-only identity for unresolved packs without inventing episodes",
        owner="identity", available="when individual episodes are verified",
        origin="AniDB episode identification"))
    attributes: dict = field(default_factory=dict, metadata=dict(
        meaning="Optional JSON-compatible series enrichment keyed by documented meaning",
        reason="Extend series information without adding workflow state",
        owner="identity", available="when known", origin="identified series data"))

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("Unsupported AnimeSeries schema version")
        if not isinstance(self.anime, Anime):
            raise ValueError("anime MUST be an identified Anime")
        if type(self.episodes) is not tuple:
            raise ValueError("episodes MUST be a tuple of identified episodes")
        if any(not isinstance(e, Episode) for e in self.episodes):
            raise ValueError("episodes MUST contain only identified Episode objects")
        ids = [e.anidb_episode_id for e in self.episodes]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate AniDB episode ID within one series")
        if type(self.attributes) is not dict or any(type(k) is not str for k in self.attributes):
            raise ValueError("attributes MUST be a JSON object with string keys")
        try:
            json.dumps(self.attributes, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("attributes MUST contain JSON-compatible values") from error

    def to_dict(self):
        return {"schema_version": self.schema_version, "anime": asdict(self.anime),
                "episodes": [asdict(e) for e in self.episodes],
                "attributes": copy.deepcopy(self.attributes)}

    @classmethod
    def from_dict(cls, value):
        def parse(kind, obj):
            if type(obj) is not dict:
                raise ValueError(f"{kind.__name__} MUST be an object")
            allowed = {f.name for f in fields(kind)}
            required = {f.name for f in fields(kind)
                        if f.default is MISSING and f.default_factory is MISSING}
            if set(obj) - allowed or required - set(obj):
                raise ValueError(f"{kind.__name__}: unknown or missing required fields")
            return kind(**obj)

        if type(value) is not dict:
            raise ValueError("AnimeSeries MUST be a JSON object")
        allowed = {"schema_version", "anime", "episodes", "attributes"}
        if set(value) - allowed or not {"schema_version", "anime"} <= set(value):
            raise ValueError("AnimeSeries: unknown or missing required fields")
        anime = parse(Anime, value["anime"])
        raw_episodes = value.get("episodes", [])
        if type(raw_episodes) is not list:
            raise ValueError("episodes MUST be an array")
        episodes = []
        for raw in raw_episodes:
            if type(raw) is not dict:
                raise ValueError("episodes[] MUST be an object")
            fields_ = dict(raw)
            placement = fields_.get("tvdb_placement")
            if placement is not None:
                fields_["tvdb_placement"] = parse(TVDBPlacement, placement)
            episodes.append(parse(Episode, fields_))
        return cls(schema_version=value["schema_version"], anime=anime,
                   episodes=tuple(episodes), attributes=value.get("attributes", {}))

    @classmethod
    def from_json(cls, text):
        return cls.from_dict(json.loads(text))

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                          allow_nan=False)
