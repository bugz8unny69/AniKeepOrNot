"""Bounded, per-attempt identity evidence for authenticated Matching details.

These are observations, never evidence that authorizes an acquisition. The
format deliberately excludes credentials, provider URLs/responses and arbitrary
exception messages. A trace is saved only with a worker's fenced job outcome.
"""
import json

STEPS = frozenset((
    "release_title", "recognized_season", "season_scope", "anidb_candidates",
    "anidb_record", "anime_lists", "tvdb_series", "tvdb_animation",
    "provider_link", "provider_titles", "identity_verified",
))
STATUSES = frozenset(("passed", "failed", "waiting", "skipped"))
NOTES = frozenset((
    "invalid_release_title", "multiple_seasons", "not_previously_recognized",
    "recognized_season_reused", "unverified_sequel", "sequel_alias_selected", "base_series_season_lookup", "season_entry_selected", "no_title_aliases",
    "no_unique_anidb_candidate", "no_anidb_candidates", "multiple_anidb_candidates", "anidb_titles_disagree", "no_anime_lists_mapping",
    "tvdb_live_action", "tvdb_not_animation", "tvdb_anidb_conflict",
    "provider_titles_disagree", "missing_verification", "verified",
    "upstream_deferred", "upstream_unavailable", "invalid_provider_response",
    "provider_auth_failed", "identity_conflict", "canceled",
))
MAX_MATCHING_BYTES = 8192


def _label(value):
    if (type(value) is not str or not value or len(value.encode("utf-8")) > 160
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise ValueError("invalid matching evidence label")
    return value


def _display(value):
    """Shorten Unicode labels by encoded bytes; do not persist raw responses."""
    if type(value) is not str or not value:
        return None
    value = value.strip()
    if not value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    while len(value.encode("utf-8")) > 160:
        value = value[:-1]
    return value or None


def _identifier(value):
    if value is not None and (type(value) is not int or not 1 <= value < 1 << 63):
        raise ValueError("invalid matching evidence identifier")


def validated(trace):
    """Validate an exact, bounded public diagnostic shape, not arbitrary JSON."""
    if type(trace) is not dict or set(trace) != {
        "version", "season", "episode", "queries", "anidb_candidates",
        "tvdb_titles", "selected_anidb_id", "selected_tvdb_id", "checks",
    } or trace["version"] != 1:
        raise ValueError("invalid matching evidence structure")
    for key in ("season", "episode"):
        val = trace[key]
        if val is not None and (type(val) is not int or not 1 <= val <= 9999):
            raise ValueError("invalid matching evidence number")
    for key in ("selected_anidb_id", "selected_tvdb_id"):
        _identifier(trace[key])
    if (type(trace["queries"]) is not list or len(trace["queries"]) > 4
            or type(trace["tvdb_titles"]) is not list or len(trace["tvdb_titles"]) > 4
            or any(_label(x) is None for x in trace["queries"] + trace["tvdb_titles"])):
        raise ValueError("invalid matching evidence titles")
    candidates = trace["anidb_candidates"]
    if type(candidates) is not list or len(candidates) > 6:
        raise ValueError("invalid matching candidates")
    for entry in candidates:
        if type(entry) is not dict or set(entry) != {"anidb_id", "titles"}:
            raise ValueError("invalid matching candidate")
        _identifier(entry["anidb_id"])
        if entry["anidb_id"] is None or type(entry["titles"]) is not list or len(entry["titles"]) > 3:
            raise ValueError("invalid matching candidate titles")
        for title in entry["titles"]:
            _label(title)
    checks = trace["checks"]
    if type(checks) is not list or len(checks) > 16:
        raise ValueError("invalid matching checks")
    for check in checks:
        if (type(check) is not dict or set(check) != {"step", "status", "note"}
                or check["step"] not in STEPS or check["status"] not in STATUSES
                or check["note"] is not None and check["note"] not in NOTES):
            raise ValueError("invalid matching check")
    data = json.dumps(trace, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(data.encode("utf-8")) > MAX_MATCHING_BYTES:
        raise ValueError("matching evidence exceeds limit")
    return data


def from_json(raw):
    if type(raw) is not str or len(raw.encode("utf-8")) > MAX_MATCHING_BYTES:
        raise ValueError("invalid matching evidence bytes")
    data = json.loads(raw)
    validated(data)
    return data


class MatchingTrace:
    def __init__(self):
        self.data = {
            "version": 1, "season": None, "episode": None, "queries": [],
            "anidb_candidates": [], "tvdb_titles": [], "selected_anidb_id": None,
            "selected_tvdb_id": None, "checks": [],
        }

    def check(self, step, status, note=None):
        if step not in STEPS or status not in STATUSES or note is not None and note not in NOTES:
            raise ValueError("invalid matching diagnostic code")
        if len(self.data["checks"]) < 16:
            self.data["checks"].append({"step": step, "status": status, "note": note})

    def queries(self, aliases, episode=None, season=None):
        self.data["queries"] = [display for x in aliases[:4] if (display := _display(x)) is not None]
        self.data["episode"] = episode if type(episode) is int and 1 <= episode <= 9999 else None
        self.data["season"] = season if type(season) is int and 1 <= season <= 9999 else None

    def candidates(self, candidates):
        items = []
        for aid, titles in list(candidates.items())[:6]:
            if type(aid) is int and 1 <= aid < 1 << 63:
                items.append({"anidb_id": aid, "titles": [
                    display for title in list(titles)[:3]
                    if (display := _display(title)) is not None
                ]})
        self.data["anidb_candidates"] = items

    def tvdb(self, tid, titles):
        self.data["selected_tvdb_id"] = tid
        self.data["tvdb_titles"] = [
            display for x in list(titles)[:4]
            if (display := _display(x)) is not None
        ]

    def anidb(self, aid):
        self.data["selected_anidb_id"] = aid

    def dump(self):
        return validated(self.data) if self.data["checks"] else None
