"""Trusted handler display registrations, separate from future dispatch code.

No provider handlers are registered by this foundation. A handler implementation
must supply its stable code and static readable label at trusted application
composition time, never from a job payload, filename or provider response.
"""
from dataclasses import dataclass
import re
from types import MappingProxyType
import unicodedata

_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}", re.ASCII)
_INVOCATION = re.compile(r"[a-f0-9]{32}", re.ASCII)


def handler_code(value):
    if type(value) is not str or not _CODE.fullmatch(value):
        raise ValueError("invalid handler code")
    return value


def invocation_id(value):
    if type(value) is not str or not _INVOCATION.fullmatch(value):
        raise ValueError("invalid handler invocation identifier")
    return value


@dataclass(frozen=True)
class HandlerDefinition:
    code: str
    label: str

    def __post_init__(self):
        handler_code(self.code)
        if self.code == "UNKNOWN":
            raise ValueError("reserved handler code")
        if (type(self.label) is not str or not self.label.strip() or len(self.label) > 128
                or any(unicodedata.category(char).startswith("C") for char in self.label)
                or any(char in self.label for char in "{}<>")):
            raise ValueError("invalid static handler label")

    @property
    def message_key(self):
        return "handler." + self.code + ".label"


class HandlerCatalog:
    def __init__(self, definitions=()):
        if type(definitions) not in (list, tuple):
            raise ValueError("invalid handler definitions")
        entries = {}
        for definition in definitions:
            if type(definition) is not HandlerDefinition or definition.code in entries:
                raise ValueError("invalid or duplicate handler registration")
            entries[definition.code] = definition
        self._entries = MappingProxyType(entries)

    def require(self, code):
        handler_code(code)
        entry = self._entries.get(code)
        if entry is None:
            raise ValueError("unregistered job handler")
        return entry

    def messages(self):
        return {entry.message_key: entry.label for entry in self._entries.values()}


EMPTY_HANDLERS = HandlerCatalog()
HANDLER_EVENTS = frozenset({"HANDLER_STARTED", "HANDLER_COMPLETED", "HANDLER_FAILED"})
