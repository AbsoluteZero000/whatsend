"""Discover one provider.py per package, with no shared registration list."""
from functools import lru_cache
from importlib import import_module
from pkgutil import iter_modules
import re

from app.providers.base import BaseSender, Provider, ProviderContext


class ProviderRegistry:
    def __init__(self):
        self._providers: dict[str, Provider] = {}

    def register(self, provider: Provider) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,19}", provider.id):
            raise ValueError("Provider IDs must be 1–20 lowercase letters, digits or underscores")
        if provider.id in self._providers:
            raise ValueError(f"Duplicate provider: {provider.id}")
        if provider.kind not in {"messaging", "publishing"}:
            raise ValueError(f"Invalid provider kind: {provider.id}")
        if provider.auth not in {"token", "managed"}:
            raise ValueError(f"Invalid authentication mode: {provider.id}")
        if provider.delivery not in {"direct", "whatsapp_worker"}:
            raise ValueError(f"Invalid delivery mode: {provider.id}")
        if provider.delivery == "whatsapp_worker" and provider.id != "native":
            raise ValueError("The WhatsApp worker delivery mode is reserved for native")
        if not provider.label or not callable(provider.create_sender):
            raise ValueError(f"Invalid provider definition: {provider.id}")
        self._providers[provider.id] = provider

    def get(self, provider_id: str) -> Provider:
        try:
            return self._providers[provider_id]
        except KeyError:
            raise ValueError(f"Unsupported provider: {provider_id}") from None

    def label(self, provider_id: str) -> str:
        definition = self._providers.get(provider_id)
        return definition.label if definition else str(provider_id)

    def all(self) -> tuple[Provider, ...]:
        return tuple(self._providers[key] for key in sorted(self._providers))

    def sender(self, provider_id: str, context: ProviderContext):
        definition = self.get(provider_id)
        if definition.kind != "messaging":
            raise ValueError(f"Use the {definition.label} publishing page to create posts")
        sender = definition.create_sender(context)
        if definition.delivery == "direct" and not isinstance(sender, BaseSender):
            raise TypeError(f"{provider_id} must create a BaseSender")
        return sender


def discover_providers(package: str = "app.providers") -> ProviderRegistry:
    registry = ProviderRegistry()
    root = import_module(package)
    for module in sorted(iter_modules(root.__path__), key=lambda item: item.name):
        if not module.ispkg or module.name.startswith("_"):
            continue
        definition = import_module(f"{package}.{module.name}.provider").provider
        if not isinstance(definition, Provider) or definition.id != module.name:
            raise ValueError(f"Provider package {module.name} must export a matching Provider")
        registry.register(definition)
    return registry


@lru_cache(maxsize=1)
def get_registry() -> ProviderRegistry:
    return discover_providers()
