"""Small, redacted per-attempt qBittorrent metadata observations.

These are *observations*, not a durable ownership receipt and not proof of
metainfo. Never store trackers, peer IPs, save paths, credentials or magnets.
"""
import re

_FIELDS = frozenset((
    "phase", "initial_state", "last_active_state", "stopped_state",
    "polls", "max_files", "resumed",
))
_PHASES = frozenset(("fetch", "not_found", "poll", "recover_stop", "stopped_inspect"))
_STATE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}\Z", re.ASCII)


def fresh(phase, *, state=None, files=0):
    """Start a fixed-size observation, with no identifying torrent details."""
    return validated({
        "phase": phase,
        "initial_state": state,
        "last_active_state": state,
        "stopped_state": None,
        "polls": 0,
        "max_files": files,
        "resumed": False,
    })


def validated(evidence):
    """Validate a bounded, version-independent history detail shape."""
    if type(evidence) is not dict or set(evidence) != _FIELDS:
        raise ValueError("invalid metadata observation")
    if evidence["phase"] not in _PHASES:
        raise ValueError("invalid metadata observation phase")
    for key in ("initial_state", "last_active_state", "stopped_state"):
        state = evidence[key]
        if state is not None and (type(state) is not str or not _STATE.fullmatch(state)):
            raise ValueError("invalid metadata observation state")
    for key, max_value in (("polls", 24), ("max_files", 10000)):
        count = evidence[key]
        if type(count) is not int or not 0 <= count <= max_value:
            raise ValueError("invalid metadata observation count")
    if type(evidence["resumed"]) is not bool:
        raise ValueError("invalid metadata observation resume flag")
    return dict(evidence)
