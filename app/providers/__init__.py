"""Provider registry and stream resolution primitives."""

from .base import (
    BaseProvider,
    ProviderAuthError,
    ProviderCapabilities,
    ProviderError,
    ProviderInteractionRequired,
    ProviderOfflineError,
    ProviderPrivateError,
    ProviderStatus,
    ResolvedStream,
)
from .registry import ProviderRegistry, create_provider_registry

__all__ = [
    "BaseProvider",
    "ProviderAuthError",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderInteractionRequired",
    "ProviderOfflineError",
    "ProviderPrivateError",
    "ProviderRegistry",
    "ProviderStatus",
    "ResolvedStream",
    "create_provider_registry",
]
