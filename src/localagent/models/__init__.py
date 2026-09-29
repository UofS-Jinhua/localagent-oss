"""Model registry, discovery, and chat clients."""

from localagent.models.openai_compat import OpenAICompatClient
from localagent.models.registry import Registry, RegistryError, Resolved


def make_client(resolved: Resolved) -> OpenAICompatClient:
    """Return a chat client for a resolved model."""
    if resolved.provider.type == "openai_compatible":
        return OpenAICompatClient(resolved.provider, resolved.model)
    raise RegistryError(f"unsupported provider type {resolved.provider.type!r}")


__all__ = ["OpenAICompatClient", "Registry", "RegistryError", "Resolved", "make_client"]
