"""Registry of service adapters. Nothing else hard-codes the list of services."""
from src.anime.services.base import ServiceAdapter, valid_name


class ServiceRegistry:
    def __init__(self, adapters=()):
        self._adapters = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter):
        """Add one adapter. Plugins call this to introduce a new service."""
        if not isinstance(adapter, ServiceAdapter):
            raise ValueError('service adapter required')
        if not valid_name(adapter.name) or type(adapter.label) is not str or not adapter.label.strip():
            raise ValueError('service adapter needs a valid name and label')
        if adapter.name in self._adapters:
            raise ValueError(f'service already registered: {adapter.name}')
        self._adapters[adapter.name] = adapter
        return adapter

    def get(self, name):
        try:
            return self._adapters[name]
        except KeyError:
            raise ValueError(f'unknown service: {name}') from None

    def names(self):
        return tuple(self._adapters)

    def __iter__(self):
        return iter(tuple(self._adapters.values()))

    def __contains__(self, name):
        return name in self._adapters

    def __len__(self):
        return len(self._adapters)


def default_registry():
    """The built-in adapters for every service Anime Discovery uses today."""
    from src.anime.services.adapters import builtin_adapters
    return ServiceRegistry(builtin_adapters())
