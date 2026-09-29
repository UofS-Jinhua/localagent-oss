"""The model registry: providers (endpoints), models, and role assignments.

Unlike KISS Sorcar, which routes by model-name prefix and supports a single
global custom endpoint, every model here names the provider it belongs to, so
any number of local and cloud endpoints can coexist.
"""

import ipaddress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from localagent.config import get_secret, registry_path

PROVIDER_TYPES = ("openai_compatible",)
THINKING_MODES = ("toggle", "always", "never")
# A role may name this instead of a model: use whatever the local server has loaded
# right now ("auto"), optionally from one provider ("auto:lmstudio"). See models/auto.py.
AUTO_MODEL = "auto"
_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale and other overlay networks
_LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".ts.net")


def is_local_url(url: str) -> bool:
    """Whether *url* points at this computer or the local network rather than a cloud service."""
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "") or host.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host  # a bare machine name on the LAN
    return address.is_private or address.is_loopback or address.is_link_local or address in _CGNAT


class RegistryError(Exception):
    """The registry file is missing or invalid."""


@dataclass
class Provider:
    """An API endpoint that serves one or more models."""

    name: str
    base_url: str
    type: str = "openai_compatible"
    api_key: str = ""
    api_key_env: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    max_concurrency: int = 2
    discovery: str = ""
    timeout: float = 900.0
    local: bool | None = None

    def resolve_api_key(self) -> str:
        """Return the literal key, else ``api_key_env`` from the environment or ``secrets.env``, else ``""``."""
        if self.api_key:
            return self.api_key
        return get_secret(self.api_key_env)

    def is_local(self) -> bool:
        """Whether requests stay on this computer or network (``local`` overrides the guess from the URL)."""
        return is_local_url(self.base_url) if self.local is None else self.local


@dataclass
class ModelSpec:
    """A model as the agent sees it: an alias bound to a provider's model id."""

    name: str
    provider: str
    model_id: str
    context_length: int = 32768
    continue_at: float = 0.65
    thinking: str = "toggle"
    thinking_off: dict[str, Any] = field(default_factory=dict)
    max_output_tokens: int = 16384
    # A session's first step plans the whole job, so it may think longer than the steps that carry it out.
    max_output_tokens_first: int = 0  # 0: the same as max_output_tokens
    # The output budget has to hold the largest file the model writes in one call; thinking needs
    # nowhere near that, so it is bounded on its own, counted while it streams. 0: only the output
    # budget bounds it. See models/thinking.py.
    max_thinking_tokens: int = 0
    price_input: float = 0.0  # USD per million input tokens (0 = free or unknown)
    price_output: float = 0.0  # USD per million output tokens
    extra_params: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    vision: bool = False  # accepts images
    switchable: bool = False  # the agent may move a running task to this model with set_model
    # Largest window a sub-agent of a task on this model may get (0: the default, 32,768). Worth raising
    # on a server whose KV pool is much larger than one conversation; see agent/subtask.py.
    max_subtask_context: int = 0
    # Read-only sub-agents a task on this model may run at the same time (1: one after another). Only worth
    # it where the server gains throughput from parallel requests; keep it below the server's slot count so
    # the caller's idle slot, with its cached conversation, is never handed to a sub-agent.
    max_parallel_subtasks: int = 1


@dataclass
class RoleSpec:
    """Which model (and thinking setting) a role such as ``main`` uses."""

    model: str
    thinking: bool | None = None


@dataclass
class Resolved:
    """Everything needed to talk to a model for one role."""

    model: ModelSpec
    provider: Provider
    thinking: bool


def is_auto(model: str) -> bool:
    """Whether a role's model means "whatever the server has loaded"."""
    return model == AUTO_MODEL or model.startswith(AUTO_MODEL + ":")


def auto_provider(model: str) -> str:
    """The provider named in ``auto:<provider>``, or ``""`` for any local one."""
    return model.partition(":")[2] if is_auto(model) else ""


def effective_thinking(model: ModelSpec, requested: bool | None) -> bool:
    """Combine a model's thinking capability with a role's preference."""
    if model.thinking == "never":
        return False
    if model.thinking == "always":
        return True
    return True if requested is None else requested


def _upgrade_model(body: dict[str, Any]) -> dict[str, Any]:
    """Read a model entry written before the thinking bound was counted in tokens.

    It was a character count, set assuming about 3.5 characters per token of English; kept at that
    rate so the bound stays where it was for English thinking, rounded to hundreds for the settings page.
    """
    chars = body.pop("max_thinking_chars", None)
    if chars and "max_thinking_tokens" not in body:
        body["max_thinking_tokens"] = max(100, round(int(chars) / 350) * 100)
    return body


def _pick(cls: type, data: dict[str, Any], **extra: Any) -> Any:
    known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    unknown = set(data) - known
    if unknown:
        raise RegistryError(f"{cls.__name__} has unknown field(s): {', '.join(sorted(unknown))}")
    return cls(**data, **extra)


@dataclass
class Registry:
    """Providers, models, and roles loaded from ``models.yaml``."""

    providers: dict[str, Provider] = field(default_factory=dict)
    models: dict[str, ModelSpec] = field(default_factory=dict)
    roles: dict[str, RoleSpec] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: Path | None = None) -> "Registry":
        """Build and validate a registry from its YAML/JSON structure."""
        try:
            providers = {
                name: _pick(Provider, dict(body), name=name)
                for name, body in (data.get("providers") or {}).items()
            }
            models = {
                name: _pick(ModelSpec, _upgrade_model(dict(body)), name=name)
                for name, body in (data.get("models") or {}).items()
            }
            roles = {
                name: _pick(RoleSpec, dict(body) if isinstance(body, dict) else {"model": body})
                for name, body in (data.get("roles") or {}).items()
                if body
            }
        except TypeError as e:
            raise RegistryError(f"invalid registry entry: {e}") from e
        registry = cls(providers=providers, models=models, roles=roles, path=path)
        problems = registry.problems()
        if problems:
            raise RegistryError("; ".join(problems))
        return registry

    @classmethod
    def load(cls, path: Path | None = None) -> "Registry":
        """Load the registry file; raise ``RegistryError`` if it is missing or invalid."""
        path = path or registry_path()
        if not path.is_file():
            raise RegistryError(
                f"no model registry at {path}. Run `la init --base-url <url>` to create one."
            )
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise RegistryError(f"{path} must contain a mapping")
        return cls.from_dict(data, path=path)

    def to_dict(self) -> dict[str, Any]:
        """Return the YAML structure (names become mapping keys)."""

        def strip(obj: Any) -> dict[str, Any]:
            body = asdict(obj)
            body.pop("name", None)
            return body

        return {
            "providers": {n: strip(p) for n, p in self.providers.items()},
            "models": {n: strip(m) for n, m in self.models.items()},
            "roles": {n: asdict(r) for n, r in self.roles.items()},
        }

    def save(self, path: Path | None = None) -> Path:
        """Write the registry to *path* (default: where it was loaded from)."""
        path = path or self.path or registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True)
        path.write_text(text, encoding="utf-8")
        self.path = path
        return path

    def problems(self) -> list[str]:
        """Return human-readable consistency problems (empty when valid)."""
        found = []
        for p in self.providers.values():
            if p.type not in PROVIDER_TYPES:
                found.append(f"provider {p.name!r}: unsupported type {p.type!r}")
            if p.max_concurrency < 1:
                found.append(f"provider {p.name!r}: max_concurrency must be >= 1")
        for m in self.models.values():
            if m.provider not in self.providers:
                found.append(f"model {m.name!r}: unknown provider {m.provider!r}")
            if m.thinking not in THINKING_MODES:
                found.append(f"model {m.name!r}: thinking must be one of {THINKING_MODES}")
            if not 0 < m.continue_at <= 1:
                found.append(f"model {m.name!r}: continue_at must be in (0, 1]")
        for name, r in self.roles.items():
            if is_auto(r.model):
                provider = auto_provider(r.model)
                if provider and provider not in self.providers:
                    found.append(f"role {name!r}: {r.model!r} names an unknown provider")
            elif r.model not in self.models:
                found.append(f"role {name!r}: unknown model {r.model!r}")
        return found

    def secret_env_names(self) -> set[str]:
        """Environment variables that hold API keys, so tools can keep them out of child processes."""
        return {p.api_key_env for p in self.providers.values() if p.api_key_env}

    def switch_targets(self, allow_cloud: bool) -> list[ModelSpec]:
        """Models the agent may move a running task to: ``switchable`` ones, cloud ones only if allowed."""
        return [
            m for m in self.models.values()
            if m.switchable and (allow_cloud or self.providers[m.provider].is_local())
        ]

    def helper(self, role: str, task_model: str | None) -> tuple[str | None, bool | None]:
        """Which model a helper of a running task uses: ``(model name or None, thinking)``.

        Helpers are the sub-agent, the reviewer and the commit-message writer. One whose role is
        plain ``auto`` follows the task when the task runs on a local model: that model is the one
        the server has ready -- it is serving the task -- and with two local servers (a home LM
        Studio and a GPU server behind an ssh tunnel) re-resolving ``auto`` would send the helper
        to whichever one answers, or fail when only the task's server is up. A cloud task keeps
        ``auto``, so cheap helper work does not start costing money; a role that names a model, or
        a provider with ``auto:<provider>``, is obeyed. ``None`` means "resolve the role".
        """
        own = self.roles.get(role)
        spec = own or self.roles.get("main")  # an unset role borrows main's thinking...
        thinking = spec.thinking if spec is not None else None
        model = self.models.get(task_model or "")
        # ...but not main's model: with main pinned, "same as main" would send the helper of a task
        # the user moved to another local model back to the pinned one, on another machine.
        follows = own is None or own.model == AUTO_MODEL
        if follows and model is not None and self.providers[model.provider].is_local():
            return model.name, thinking
        return None, thinking

    def resolve(self, role: str = "main", model: str | None = None) -> Resolved:
        """Pick the model for *role*, or the explicitly named *model*."""
        requested: bool | None = None
        if model is None:
            spec = self.roles.get(role) or self.roles.get("main")
            if spec is not None:
                model, requested = spec.model, spec.thinking
            elif self.models:
                model = next(iter(self.models))
            else:
                raise RegistryError("the registry has no models")
        if is_auto(model):
            from localagent.models.auto import loaded_model  # local: auto.py imports this module

            m = loaded_model(self, auto_provider(model))
        elif model not in self.models:
            known = ", ".join([*self.models, AUTO_MODEL]) or "(none)"
            raise RegistryError(f"unknown model {model!r}; known models: {known}")
        else:
            from localagent.models.sync import refreshed  # local: sync.py imports this module

            m = refreshed(self, self.models[model])
        return Resolved(model=m, provider=self.providers[m.provider], thinking=effective_thinking(m, requested))
