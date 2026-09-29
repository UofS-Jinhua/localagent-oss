"""Keep the registry's local models in step with the server.

What a local server serves is not a setting: models get downloaded and deleted
in LM Studio, and the registry should follow rather than be maintained by hand.
So before anything shows or picks from the model list, the local providers are
asked again — new models are added, ones the server no longer has are dropped.

Cloud providers are left alone: their catalogues are long, listing them costs a
request, and the handful of models the user picked is the point.
"""

import time
from dataclasses import dataclass, field, replace

from localagent.models.discovery import DiscoveredModel, discover, loaded_models
from localagent.models.presets import chat_models, spec_from_discovery
from localagent.models.registry import ModelSpec, Provider, Registry

SYNC_TTL = 30.0  # a refresh this recent is reused; the UI asks on every render
_last: dict[str, float] = {}


@dataclass
class SyncResult:
    """What changed in the registry, for a one-line notice."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # gone from the server, but a role points at them
    updated: list[str] = field(default_factory=list)  # settings the server knows better than we do
    unreachable: list[str] = field(default_factory=list)

    def changed(self) -> bool:
        return bool(self.added or self.removed or self.updated)

    def describe(self) -> str:
        parts = []
        if self.added:
            parts.append(f"added {', '.join(self.added)}")
        if self.removed:
            parts.append(f"removed {', '.join(self.removed)} (no longer on the server)")
        if self.kept:
            parts.append(f"kept {', '.join(self.kept)} although the server no longer has it: a role points at it")
        return "; ".join(parts)


def forget_sync() -> None:
    """Drop the per-provider cooldown (tests, and after the user edits a provider)."""
    _last.clear()


def _role_models(registry: Registry) -> set[str]:
    return {role.model for role in registry.roles.values()}


def _without_load_state(notes: str) -> str:
    return ", ".join(part for part in notes.split(", ") if part.strip() != "loaded")


def _sync_provider(registry: Registry, provider: Provider, result: SyncResult, timeout: float) -> None:
    _last[provider.name] = time.monotonic()  # also on failure: a dead server must not stall every call
    try:
        found = chat_models(discover(provider, timeout=timeout))
    except (ConnectionError, OSError):
        result.unreachable.append(provider.name)
        return
    served = {m.id for m in found}
    known = {spec.model_id: name for name, spec in registry.models.items() if spec.provider == provider.name}
    for model in found:
        name = known.get(model.id)
        if name is None:
            spec = spec_from_discovery(model, provider)
            name = spec.name if spec.name not in registry.models else f"{provider.name}/{spec.name}"
            registry.models[name] = replace(spec, name=name)
            result.added.append(name)
            continue
        spec = registry.models[name]
        notes = _without_load_state(spec.notes)  # older registries wrote "loaded" here; it is live state
        if notes != spec.notes:
            registry.models[name] = replace(spec, notes=notes)
            result.updated.append(name)
    in_use = _role_models(registry)
    for model_id, name in known.items():
        if model_id in served:
            continue
        if name in in_use:  # removing it would leave a role pointing at nothing
            result.kept.append(name)
            continue
        del registry.models[name]
        result.removed.append(name)


def sync_local_models(registry: Registry, *, save: bool = True, timeout: float = 4.0,
                      force: bool = False) -> SyncResult:
    """Add models the local servers gained and drop the ones they lost.

    Args:
        save: Write ``models.yaml`` back when something changed.
        timeout: Per provider; an unreachable server leaves its models alone.
        force: Ask again even if this provider was refreshed within ``SYNC_TTL``.
    """
    result = SyncResult()
    now = time.monotonic()
    for provider in list(registry.providers.values()):
        if not provider.is_local():
            continue
        if not force and now - _last.get(provider.name, 0.0) < SYNC_TTL:
            continue
        _sync_provider(registry, provider, result, timeout)
    if save and result.changed():
        registry.save()
    return result


def refreshed(registry: Registry, spec: ModelSpec) -> ModelSpec:
    """*spec* with the numbers the server reports, if it has that model loaded.

    A registry entry records what the model looked like when it was added, but
    LM Studio can reload the same model with a different context length. The
    reading is cached for a few seconds, so a named model stays as truthful as
    ``auto`` without costing a request per step.
    """
    provider = registry.providers.get(spec.provider)
    if provider is None or not provider.is_local():
        return spec
    try:
        found = next((m for m in loaded_models(provider) if m.id == spec.model_id), None)
    except (ConnectionError, OSError):
        return spec  # offline: the stored numbers are all we have
    if found is None:
        return spec  # a different model is loaded; this one keeps what the registry says
    return replace(spec, context_length=found.context_length or spec.context_length,
                   vision=spec.vision or bool(found.vision))


def loaded_now(registry: Registry, timeout: float = 2.0) -> dict[tuple[str, str], DiscoveredModel]:
    """Which registered models the local servers hold right now, by ``(provider, model id)``.

    It matters on a single-GPU server: picking a model that is not loaded makes
    the server load it, evicting the one that is.
    """
    found: dict[tuple[str, str], DiscoveredModel] = {}
    for provider in registry.providers.values():
        if not provider.is_local():
            continue
        try:
            for model in loaded_models(provider, timeout=timeout):
                found[(provider.name, model.id)] = model
        except (ConnectionError, OSError):
            continue  # the server is off; nothing is loaded as far as we know
    return found
