"""Provider registry.

Register a new shop here and it shows up in the UI, the watch editor and the
scheduler without touching anything else.
"""
from __future__ import annotations

from .amiami import AmiAmiProvider
from .base import ShopProvider

_PROVIDERS: dict[str, ShopProvider] = {}


def _register(provider: ShopProvider) -> None:
    _PROVIDERS[provider.id] = provider


_register(AmiAmiProvider())

DEFAULT_PROVIDER = AmiAmiProvider.id


def get_provider(provider_id: str | None = None) -> ShopProvider:
    key = provider_id or DEFAULT_PROVIDER
    try:
        return _PROVIDERS[key]
    except KeyError:
        raise KeyError("Unknown provider: " + str(provider_id)) from None


def all_providers() -> list[ShopProvider]:
    return list(_PROVIDERS.values())


def provider_ids() -> list[str]:
    return list(_PROVIDERS)


def detect_provider_from_url(url: str) -> tuple[str, str] | None:
    """Return (provider_id, item_code) for a pasted product URL."""
    for provider in _PROVIDERS.values():
        code = provider.parse_url(url)
        if code:
            return provider.id, code
    return None


def close_all() -> None:
    for provider in _PROVIDERS.values():
        provider.close()
