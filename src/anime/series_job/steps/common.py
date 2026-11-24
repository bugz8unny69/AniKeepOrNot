"""Shared step types and results for the series job."""
from dataclasses import dataclass

STEP_SEQUENCE = (
    "check_release",
    "reserve_episodes",
    "download",
    "check_media",
    "transcode",
    "stage",
)


@dataclass(frozen=True)
class StepResult:
    action: str  # "done", "wait", "drop"
    next_step: str | None = None
    wake_at_ms: int | None = None
    why: str | None = None

    @classmethod
    def done(cls, next_step: str | None = None):
        return cls(action="done", next_step=next_step)

    @classmethod
    def wait(cls, until_ms: int | None = None, why: str | None = None):
        return cls(action="wait", wake_at_ms=until_ms, why=why)

    @classmethod
    def drop(cls, why: str):
        return cls(action="drop", why=why)


def next_step_name(current_step: str) -> str:
    """Return the name of the next step following current_step, or 'done' if at the end."""
    try:
        idx = STEP_SEQUENCE.index(current_step)
        if idx + 1 < len(STEP_SEQUENCE):
            return STEP_SEQUENCE[idx + 1]
    except ValueError:
        pass
    return "done"
