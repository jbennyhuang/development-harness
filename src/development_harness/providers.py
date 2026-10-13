"""Extensible registry of independently implemented SDK Agent providers."""
from .adapters.antigravity import provider as antigravity_provider
from .adapters.codex import provider as codex_provider
from .domain import HarnessError
from .provider_types import AdapterFactory

ADAPTERS: dict[str, AdapterFactory] = {
    "codex": codex_provider, "agy": antigravity_provider,
}


def resolve_adapter_factory(provider: str) -> AdapterFactory:
    factory = ADAPTERS.get(provider)
    if factory is None:
        raise HarnessError("unknown_provider", "Coding provider is not registered", provider=provider)
    return factory
