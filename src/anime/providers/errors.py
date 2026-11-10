"""Safe provider outcomes shared by clients and job adapters."""
from src.anime.configuration_schema import ConfigurationError

class ProviderUnavailable(RuntimeError):
    reason_code = "SERVICE_UNAVAILABLE"
    def __init__(self):
        super().__init__("An information provider is temporarily unavailable.")


class ProviderDeferred(RuntimeError):
    reason_code = "JOB_WAITING"
    def __init__(self, delay_ms):
        if type(delay_ms) is not int or not 1 <= delay_ms <= 86400000:
            raise ValueError("invalid provider wait")
        self.delay_ms = delay_ms
        super().__init__("Work is waiting for a prerequisite.")


class ProviderInvalidResponse(RuntimeError):
    reason_code = "SERVICE_RESPONSE_INVALID"
    def __init__(self):
        super().__init__("An information provider returned an unusable response.")


class ProviderCacheMiss(RuntimeError):
    """A cache-only read found no fresh cached response; nothing was requested."""
    def __init__(self):
        super().__init__("The provider data is not cached.")


class ProviderAuthenticationFailed(RuntimeError):
    reason_code = "SERVICE_SETUP_INVALID"
    service_status = "AUTH_FAILED"
    def __init__(self):
        super().__init__("An information provider could not authorize access.")



class ProviderConfigurationError(ConfigurationError):
    reason_code = "SERVICE_SETUP_INVALID"
    service_status = "NOT_CONFIGURED"
    def __init__(self):
        ValueError.__init__(self,"Provider configuration is incomplete or invalid.")
