"""REST routes for managing the model registry from the UI: providers, models, roles, keys, tests.

API keys typed into the UI are written to ``~/.localagent/secrets.env`` under the
provider's ``api_key_env`` name; ``models.yaml`` only names the variable, and no
route ever returns a key.
"""

import threading
from dataclasses import asdict, fields
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from localagent.config import get_secret, registry_path, save_secret
from localagent.models import make_client
from localagent.models.auto import loaded_model
from localagent.models.discovery import discover, is_lmstudio
from localagent.models.presets import PRESETS, chat_models, normalize_base_url, spec_from_discovery
from localagent.models.probe import run_quick_probes
from localagent.models.sync import forget_sync, sync_local_models
from localagent.models.registry import (
    AUTO_MODEL,
    THINKING_MODES,
    ModelSpec,
    Provider,
    Registry,
    RegistryError,
    RoleSpec,
    effective_thinking,
)

ROLES = ("main", "commit_message", "subagent", "summarizer")
_EDITABLE_MODEL = {f.name for f in fields(ModelSpec)} - {"name"}
_lock = threading.Lock()  # one registry edit at a time


def _load() -> Registry:
    try:
        return Registry.load()
    except RegistryError:
        if registry_path().is_file():
            raise
        return Registry()


def _save(registry: Registry) -> None:
    problems = registry.problems()
    if problems:
        raise HTTPException(400, "; ".join(problems))
    registry.save()


def _provider_view(p: Provider) -> dict[str, Any]:
    return {
        "name": p.name, "base_url": p.base_url, "type": p.type, "api_key_env": p.api_key_env,
        "has_key": bool(p.resolve_api_key()), "max_concurrency": p.max_concurrency, "discovery": p.discovery,
        "local": p.is_local(), "timeout": p.timeout,
    }


def registry_view(registry: Registry) -> dict[str, Any]:
    """The registry as the UI sees it (keys replaced by ``has_key``)."""
    return {
        "path": str(registry.path or registry_path()),
        "providers": [_provider_view(p) for p in registry.providers.values()],
        "models": [{**asdict(m), "local": registry.providers[m.provider].is_local()} for m in registry.models.values()],
        "roles": {name: asdict(role) for name, role in registry.roles.items()},
        "role_names": list(ROLES),
        "thinking_modes": list(THINKING_MODES),
        "presets": [p.to_dict() for p in PRESETS],
        "auto_model": _auto_model(registry),
        "auto_label": AUTO_MODEL,
    }


def _auto_model(registry: Registry) -> str | None:
    """What ``auto`` currently points at, so the UI can show "auto → <model>"."""
    try:
        return loaded_model(registry).name
    except (RegistryError, ConnectionError, OSError):
        return None


def _used_by_roles(registry: Registry, models: set[str]) -> list[str]:
    return [name for name, role in registry.roles.items() if role.model in models]


def make_router(auth: Any) -> APIRouter:
    """The registry routes, all behind the *auth* dependency."""
    router = APIRouter(prefix="/api", dependencies=[auth])

    @router.get("/registry")
    def get_registry() -> dict[str, Any]:
        registry = _load()
        sync_local_models(registry)  # a model downloaded or deleted in LM Studio shows up here
        return registry_view(registry)

    @router.put("/providers/{name}")
    def put_provider(name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Create or update a provider. ``api_key`` (if given) goes to secrets.env, never to models.yaml."""
        if not name.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(400, "provider names may only use letters, digits, - and _")
        with _lock:
            registry = _load()
            old = registry.providers.get(name)
            base_url = normalize_base_url(str(body.get("base_url") or (old.base_url if old else "")))
            if not base_url.startswith(("http://", "https://")):
                raise HTTPException(400, "the base URL must start with http:// or https://")
            key_env = str(body.get("api_key_env") or (old.api_key_env if old else "")).strip()
            api_key = str(body.get("api_key") or "").strip()
            if api_key and not key_env:
                key_env = f"LA_{name.upper().replace('-', '_')}_API_KEY"
            discovery = body.get("discovery", old.discovery if old else "")
            if discovery == "auto":
                discovery = "lmstudio" if is_lmstudio(base_url) else ""
            provider = Provider(
                name=name, base_url=base_url, api_key_env=key_env,
                headers=old.headers if old else {},
                max_concurrency=int(body.get("max_concurrency") or (old.max_concurrency if old else 2)),
                discovery=str(discovery or ""), timeout=float(body.get("timeout") or (old.timeout if old else 900)),
                local=body["local"] if "local" in body else (old.local if old else None),
            )
            if api_key:
                try:
                    save_secret(key_env, api_key)
                except ValueError as e:
                    raise HTTPException(400, str(e)) from None
            registry.providers[name] = provider
            forget_sync()  # its address or key may have just changed; ask it again now
            _save(registry)
            return registry_view(registry)

    @router.delete("/providers/{name}")
    def delete_provider(name: str, forget_key: bool = False) -> dict[str, Any]:
        with _lock:
            registry = _load()
            provider = registry.providers.get(name)
            if provider is None:
                raise HTTPException(404, f"no provider {name!r}")
            models = {m.name for m in registry.models.values() if m.provider == name}
            roles = _used_by_roles(registry, models)
            if roles:
                raise HTTPException(409, f"roles {', '.join(roles)} use its models; assign other models first")
            for model in models:
                del registry.models[model]
            del registry.providers[name]
            _save(registry)
            if forget_key and provider.api_key_env and get_secret(provider.api_key_env):
                save_secret(provider.api_key_env, "")
            return registry_view(registry)

    @router.get("/providers/{name}/discover")
    def discover_models(name: str) -> dict[str, Any]:
        registry = _load()
        provider = registry.providers.get(name)
        if provider is None:
            raise HTTPException(404, f"no provider {name!r}")
        try:
            found = chat_models(discover(provider))
        except ConnectionError as e:
            raise HTTPException(502, str(e)) from None
        registered = {m.model_id for m in registry.models.values() if m.provider == name}
        return {"models": [{**asdict(spec_from_discovery(m, provider)), "loaded": m.loaded,
                            "registered": m.id in registered} for m in found]}

    @router.put("/models/{name:path}")
    def put_model(name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        with _lock:
            registry = _load()
            old = registry.models.get(name)
            data = asdict(old) if old else {"name": name, "provider": "", "model_id": name}
            unknown = set(body) - _EDITABLE_MODEL - {"name"}
            if unknown:
                raise HTTPException(400, f"unknown model field(s): {', '.join(sorted(unknown))}")
            data.update({k: v for k, v in body.items() if k in _EDITABLE_MODEL})
            try:
                spec = ModelSpec(**data)
            except TypeError as e:
                raise HTTPException(400, str(e)) from None
            registry.models[name] = spec
            if "main" not in registry.roles:  # the first model becomes the main one
                registry.roles["main"] = RoleSpec(model=name, thinking=True)
            _save(registry)
            return registry_view(registry)

    @router.delete("/models/{name:path}")
    def delete_model(name: str) -> dict[str, Any]:
        with _lock:
            registry = _load()
            if name not in registry.models:
                raise HTTPException(404, f"no model {name!r}")
            roles = _used_by_roles(registry, {name})
            if roles:
                raise HTTPException(409, f"roles {', '.join(roles)} use {name}; assign another model first")
            del registry.models[name]
            _save(registry)
            return registry_view(registry)

    @router.put("/roles")
    def put_roles(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Set roles: ``{"main": {"model": "...", "thinking": true}, "subagent": null, ...}``."""
        with _lock:
            registry = _load()
            for role, value in body.items():
                if not value:
                    if role == "main":
                        raise HTTPException(400, "the main role needs a model")
                    registry.roles.pop(role, None)
                    continue
                if not isinstance(value, dict) or "model" not in value:
                    raise HTTPException(400, f"role {role!r} needs a model")
                registry.roles[role] = RoleSpec(model=str(value["model"]), thinking=value.get("thinking"))
            _save(registry)
            return registry_view(registry)

    @router.post("/models/{name:path}/test")
    def test_model(name: str) -> dict[str, Any]:
        """One tool-call request (plus one image request for vision models)."""
        registry = _load()
        if name not in registry.models:
            raise HTTPException(404, f"no model {name!r}")
        resolved = registry.resolve(model=name)
        resolved.thinking = effective_thinking(resolved.model, False)
        client = make_client(resolved)
        try:
            results = run_quick_probes(client)
        finally:
            client.close()
        return {"model": name, "results": [asdict(r) for r in results]}

    return router
