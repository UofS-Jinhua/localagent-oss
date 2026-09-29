"""``auto``: use whichever model the local server currently has loaded.

A one-GPU server holds one model at a time, and which one it is changes in LM
Studio, not here. Pinning a role to a model name means the next task quietly
makes the server swap models; ``auto`` follows the server instead, and picks up
the context length it was actually loaded with.
"""

from dataclasses import replace

from localagent.models.discovery import DiscoveredModel, loaded_models
from localagent.models.presets import chat_models, spec_from_discovery
from localagent.models.registry import AUTO_MODEL, ModelSpec, Provider, Registry, RegistryError


def _registered(registry: Registry, provider: Provider, found: DiscoveredModel) -> ModelSpec | None:
    return next((s for s in registry.models.values()
                 if s.provider == provider.name and s.model_id == found.id), None)


def loaded_model(registry: Registry, provider_name: str = "") -> ModelSpec:
    """The model a local provider has loaded, as a ``ModelSpec``.

    Args:
        provider_name: Ask only this provider (from ``auto:<provider>``);
            by default every local provider is tried in registry order.

    Raises:
        RegistryError: No local provider could be asked, or none has a model loaded.
    """
    if provider_name:
        if provider_name not in registry.providers:
            raise RegistryError(f"{AUTO_MODEL}:{provider_name} names a provider that is not in the registry")
        providers = [registry.providers[provider_name]]
    else:
        providers = [p for p in registry.providers.values() if p.is_local()]
    if not providers:
        raise RegistryError(f"{AUTO_MODEL!r} needs a local provider in the registry")

    problems = []
    for provider in providers:
        try:
            found = chat_models(loaded_models(provider))
        except ConnectionError as e:
            problems.append(str(e))
            continue
        if not found:
            problems.append(f"{provider.name} has no model loaded")
            continue
        best = found[0]
        spec = _registered(registry, provider, best)
        if spec is None:  # loaded but never registered: usable right away
            return spec_from_discovery(best, provider)
        # The server knows these two better than the registry does.
        return replace(spec, context_length=best.context_length or spec.context_length,
                       vision=spec.vision or bool(best.vision))
    raise RegistryError(f"cannot resolve {AUTO_MODEL!r}: " + "; ".join(problems)
                        + ". Load a model on the server, or pick a model by name.")
