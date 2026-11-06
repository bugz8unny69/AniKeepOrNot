"""Service health foundation: one adapter per external service, a registry and a health monitor."""
from src.nextgen.services.base import (
    AUTH_FAILED, DISABLED, DOWN, NOT_A_FAILURE, NOT_CONFIGURED, STATUSES, UNKNOWN, UNSUPPORTED, UP, USABLE,
    ProbeResult, ServiceAdapter,
)
from src.nextgen.services.health import ServiceHealth, ServiceState
from src.nextgen.services.registry import ServiceRegistry, default_registry
from src.nextgen.services import status_store

__all__ = (
    'AUTH_FAILED', 'DISABLED', 'DOWN', 'NOT_A_FAILURE', 'NOT_CONFIGURED', 'STATUSES', 'UNKNOWN', 'UNSUPPORTED',
    'UP', 'USABLE', 'ProbeResult', 'ServiceAdapter', 'ServiceHealth', 'ServiceState',
    'ServiceRegistry', 'default_registry', 'status_store',
)
