"""Shared English diagnostic catalog and translation-ready presentation.

Static sentences only. Translation maps are trusted application resources,
not provider/user input. No locale loader or dashboard API is implemented.
"""
from dataclasses import dataclass
from types import MappingProxyType
import re
import unicodedata

from src.anime.handlers import EMPTY_HANDLERS, HandlerCatalog, HandlerDefinition
from src.anime.reasons import REGISTRY, display_reason, get_reason


@dataclass(frozen=True)
class EventDefinition:
    code: str
    scope: str
    message: str
    reason_required: bool = False
    exact_reason: str | None = None


_EVENTS = (
    EventDefinition('EPISODE_RESERVATIONS_RELEASED','job','Canceled episodes were released after cleanup.',True,'CANCELED_RESERVATIONS_RELEASED'),
    EventDefinition('KEEP_COMPLETED','job','Episode kept in the library.',True),
    EventDefinition('MANAGEMENT_ACTION_RESUMED','job','The action was submitted again after review.'),
    EventDefinition('SERIES_REJECTED','job','Anime rejected; no more episodes are downloaded.',True,'ANIME_REJECTED'),
    EventDefinition('EPISODE_SEARCH_RESET','job','Failed episode attempts were cleaned up; searching again.'),
    EventDefinition('API_SESSION_CREATED','runtime','Signed in to the dashboard.'),
    EventDefinition('API_SESSION_CLOSED','runtime','Signed out of the dashboard.'),
    EventDefinition('API_CREDENTIALS_UPDATED','runtime','Service credentials were updated.'),
    EventDefinition('API_REQUEST_REJECTED','runtime','An API request was refused.',True),
    EventDefinition('API_TRANSPORT_NOTICE','runtime','The API connection needs attention.'),
    EventDefinition('ACCEPTED','incoming_event','Release received.'),
    EventDefinition('SERIES_IDENTIFIED','incoming_event','Anime identified.'),
    EventDefinition('CREATED','job','Job created.'),
    EventDefinition('CLAIMED','job','Job started.'),
    EventDefinition('LEASE_EXPIRED','job','The job was interrupted before it finished.',True,'JOB_LEASE_EXPIRED'),
    EventDefinition('DEFERRED','job','Waiting; the job is checked again later.',True),
    EventDefinition('RETRY_SCHEDULED','job','Another attempt is scheduled.',True),
    EventDefinition('RETRY_REQUESTED','job','Retry requested.'),
    EventDefinition('NEEDS_REVIEW','job','Stopped; needs review.',True),
    EventDefinition('TORRENT_SELECTION_RECORDED','job','Asked qBittorrent to download only the chosen file.',True),
    EventDefinition('TORRENT_SELECTION_CONFIRMED','job','qBittorrent confirmed the chosen file.',True),
    EventDefinition('TORRENT_TRANSFER_CHANGED','job','Download status changed.',True),
    EventDefinition('TORRENT_SUBMISSION_RECORDED','job','About to add the torrent to qBittorrent.',True),
    EventDefinition('TORRENT_RECONCILED','job','Checked the torrent in qBittorrent.',True),
    EventDefinition('ACQUISITION_PREPARED','job','Torrent files checked; download planned.',True),
    EventDefinition('EPISODE_RESERVATION_CREATED','job','Episode reserved.',True),
    EventDefinition('EPISODE_RESERVATION_SKIPPED','job','No episode reservation was needed.',True),
    EventDefinition('USER_EVALUATION_CHECKED','job','Checked whether this episode is picked for your evaluation.',True),
    EventDefinition('USER_EVALUATION_AUDIO_SEARCH','job','Release has no Dual Audio tag; searching Prowlarr instead.',True,'RELEASE_AUDIO_UNTAGGED'),
    EventDefinition('EPISODE_DISCOVERY_RECORDED','job','Episode search results saved.'),
    EventDefinition('DONE','job','Job finished.'),
    EventDefinition('FAILED','job','Job failed.',True),
    EventDefinition('ON_HOLD','job','Job on hold.',True),
    EventDefinition('BLOCKED','job','Job blocked.',True),
    EventDefinition('DECISION_DOWNLOAD_CONTROL_RECORDED','job','Download cleanup step saved.',True),
    EventDefinition('CANCELLATION_CONTROL_RECORDED','job','Cancellation cleanup step saved.',True),
    EventDefinition('CANCELLATION_REQUESTED','job','Cancellation requested.',True),
    EventDefinition('CANCELLATION_CLEANUP_CHANGED','job','Cancellation cleanup status changed.',True),
    EventDefinition('CANCELED','job','Job canceled.',True),
    EventDefinition('HANDLER_STARTED','job','Step started.'),
    EventDefinition('HANDLER_COMPLETED','job','Step finished.'),
    EventDefinition('HANDLER_FAILED','job','Step failed.',True),
    EventDefinition('CONFIGURATION_SAVED','runtime','Settings saved.'),
    EventDefinition('CONFIGURATION_MIGRATED','runtime','Settings upgraded.'),
    EventDefinition('CONFIGURATION_SAVE_UNCONFIRMED','runtime','Settings saved, but the disk did not confirm it.',True,'CONFIGURATION_DURABILITY_UNCONFIRMED'),
    EventDefinition('DATABASE_MIGRATED','runtime','Database upgraded.'),
    EventDefinition('MAINTENANCE_REQUESTED','runtime','New work paused for maintenance.'),
    EventDefinition('MAINTENANCE_COMPLETED','runtime','Maintenance finished; new work can start.'),
    # A runtime observation, not a persisted job state or lifecycle transition.
    EventDefinition('OPERATION_FAILED','runtime','An operation failed.',True),
    # DEBUG only (JD-1d): each check a job runs, and why a step did not finish.
    EventDefinition('CHECK_PASSED','runtime','Check passed.'),
    EventDefinition('CHECK_FAILED','runtime','Check failed.'),
    EventDefinition('STEP_NOT_FINISHED','runtime','Not finished.',True),
    EventDefinition('SERVICE_DOWN','runtime','A service stopped working; jobs that need it wait.'),
    EventDefinition('SERVICE_RESTORED','runtime','A service is working again; waiting jobs continue.'),
    EventDefinition('SERVICE_STARTUP_REFUSED','runtime','The worker did not start: a required service is not set up correctly.',True,'SERVICE_SETUP_INVALID'),
)
EVENTS = MappingProxyType({item.code: item for item in _EVENTS})
JOB_ACTIVITIES = MappingProxyType({
    "DOWNLOADING": "Downloading",
    "WAITING_FOR_DOWNLOAD": "Waiting for download",
    "WAITING_FOR_SEARCH": "Waiting for search",
    "SKIPPED_DUPLICATE": "Skipped duplicate",
})
_LABELS = {"PENDING": "Queued", "RUNNING": "Running",
           "RETRY_WAIT": "Waiting for another attempt", "NEEDS_REVIEW": "Needs review"}
STATES = MappingProxyType({
    "acquisition": MappingProxyType({"ACTIVE":"Not staged", "STAGED":"Staged",
                                     "CANCEL_REQUESTED":"Cancellation requested", "CANCELED":"Canceled"}),
    "job": MappingProxyType({**_LABELS, "DONE": "Step completed", "ON_HOLD": "On hold",
                             "BLOCKED": "Blocked", "CANCELED": "Canceled", "FAILED": "Step failed"}),
    "job_activity": JOB_ACTIVITIES,
    "job_result": MappingProxyType({"IDENTIFY_ANIME":"Series identified", "VERIFY_RELEASE":"Episode verified",
        "SELECT_USER_EVALUATION":"Episode selection checked", "RESERVE_EPISODE":"Episode reserved",
        "PREPARE_ACQUISITION":"Download prepared", "SUBMIT_TORRENT":"Torrent submitted",
        "DOWNLOAD_TORRENT":"Download finished", "VERIFY_MEDIA":"Video checked",
        "STAGE_USER_EVALUATION_MEDIA":"Staged"}),
    "incoming_event": MappingProxyType({**_LABELS, "PENDING": "Received",
                                       "RUNNING": "Identifying", "IDENTIFIED": "Series identified"}),
})
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}", re.ASCII)


def safe_code(code):
    return code if type(code) is str and _CODE.fullmatch(code) else "UNKNOWN"


def validate_job_activity(state, activity_code):
    """Validate stored shape, not worker transition or claim policy."""
    if type(state) is not str or state not in STATES["job"]:
        raise ValueError("invalid job state")
    if activity_code is None:
        return
    if type(activity_code) is not str or activity_code not in JOB_ACTIVITIES:
        raise ValueError("invalid job activity")
    allowed = {"DOWNLOADING": {"RUNNING", "RETRY_WAIT"},
               "WAITING_FOR_DOWNLOAD": {"RETRY_WAIT"},
               "WAITING_FOR_SEARCH": {"RETRY_WAIT"}, "SKIPPED_DUPLICATE": {"DONE"}}
    if state not in allowed[activity_code]:
        raise ValueError("job activity does not match state")


def validate_event(code, reason_code=None, *, scope=None):
    definition = EVENTS.get(code) if type(code) is str else None
    if definition is None or (scope is not None and definition.scope != scope):
        raise ValueError("unregistered event or invalid scope")
    if definition.reason_required:
        if get_reason(reason_code) is None:
            raise ValueError("registered reason required")
        if definition.exact_reason and reason_code != definition.exact_reason:
            raise ValueError("invalid event reason")
    elif reason_code is not None:
        raise ValueError("reason not allowed for event")
    return definition


def _english():
    values = {f"event.{item.code}.message": item.message for item in _EVENTS}
    for code, reason in REGISTRY.items():
        values[f"reason.{code}.message"] = reason.message
        values[f"reason.{code}.action"] = reason.action
    for entity, states in STATES.items():
        for code, label in states.items():
            values[f"state.{entity}.{code}.label"] = label
    values.update({
        "event.UNKNOWN.message": "Additional processing activity was recorded.",
        "reason.UNKNOWN.message": "Additional processing information is available.",
        "reason.UNKNOWN.action": "Review the job history.",
        "state.UNKNOWN.label": "Unknown status",
        "handler.UNKNOWN.label": "Unknown job handler",
    })
    return values


def _validate_text(text):
    if (type(text) is not str or not text.strip() or len(text) > 512
            or any(unicodedata.category(char).startswith("C") for char in text)
            or any(char in text for char in "{}<>")):
        raise ValueError("invalid static catalog message")


ENGLISH = MappingProxyType(_english())
if len(EVENTS) != len(_EVENTS):
    raise ValueError("duplicate event code")
for _text in ENGLISH.values():
    _validate_text(_text)


class MessageCatalog:
    """Immutable snapshot of optional trusted locale maps; English fallback.

    No interpolation, file watching or network loading. Unknown keys and unsafe
    translations fail validation before changing any active application state.
    """
    def __init__(self, translations=None, *, handlers=EMPTY_HANDLERS):
        if type(handlers) is not HandlerCatalog:
            raise ValueError("invalid handler catalog")
        self._handlers = handlers
        self._english = MappingProxyType({**ENGLISH, **handlers.messages()})
        if translations is None:
            translations = {}
        if type(translations) is not dict:
            raise ValueError("invalid translations")
        snapshot = {}
        for locale, entries in translations.items():
            if (type(locale) is not str or not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", locale)
                    or locale == "en" or type(entries) is not dict):
                raise ValueError("invalid translation locale")
            for key, text in entries.items():
                if type(key) is not str or key not in self._english:
                    raise ValueError("unregistered translation key")
                _validate_text(text)
            snapshot[locale] = MappingProxyType(dict(entries))
        self._translations = MappingProxyType(snapshot)

    def text(self, key, *, locale="en"):
        if type(key) is not str or key not in self._english:
            raise ValueError("unregistered message key")
        selected = self._translations.get(locale, {}) if type(locale) is str else {}
        return selected.get(key, self._english[key])

    def event(self, code, *, locale="en"):
        known = type(code) is str and code in EVENTS
        key = f"event.{code if known else 'UNKNOWN'}.message"
        return {"code": safe_code(code), "message_key": key,
                "message": self.text(key, locale=locale)}

    def handler(self, code, *, recorded_label=None, locale="en"):
        """Current translations, or a validated historical static label snapshot."""
        try:
            entry = self._handlers.require(code)
        except ValueError:
            entry = None
        if entry is not None:
            return {"code": entry.code, "message_key": entry.message_key,
                    "message": self.text(entry.message_key, locale=locale)}
        try:
            historical = HandlerDefinition(code, recorded_label)
        except ValueError:
            return {"code": safe_code(code), "message_key": "handler.UNKNOWN.label",
                    "message": self.text("handler.UNKNOWN.label", locale=locale)}
        return {"code": historical.code, "message_key": historical.message_key,
                "message": historical.label}

    def reason(self, code, *, locale="en"):
        result = display_reason(code)
        known = get_reason(code) is not None
        prefix = f"reason.{code if known else 'UNKNOWN'}"
        result.update(message_key=prefix + ".message", action_key=prefix + ".action",
                      message=self.text(prefix + ".message", locale=locale),
                      action=self.text(prefix + ".action", locale=locale))
        return result

    def state(self, entity, code, *, locale="en"):
        states = STATES.get(entity, {}) if type(entity) is str else {}
        known = type(code) is str and code in states
        key = f"state.{entity}.{code}.label" if known else "state.UNKNOWN.label"
        return {"code": safe_code(code), "message_key": key,
                "message": self.text(key, locale=locale)}

    def job_status(self, state, activity_code=None, *, locale="en", kind=None):
        """Shared read-only display selection; tolerate unknown historical data.

        Terminal/held/review states cannot be masked by a stale activity label.
        New writes must use validate_job_activity instead of this fallback.
        """
        base = self.state("job", state, locale=locale)
        try:
            validate_job_activity(state, activity_code)
        except ValueError:
            return base
        if state == 'DONE' and activity_code is None and kind in STATES['job_result']:
            label=self.state('job_result',kind,locale=locale)
            return {**label,'code':'DONE'}
        return (self.state("job_activity", activity_code, locale=locale)
                if activity_code is not None else base)


DEFAULT_CATALOG = MessageCatalog()
