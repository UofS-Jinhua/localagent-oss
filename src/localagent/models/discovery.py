"""Discover the models an endpoint serves, with LM Studio metadata when available."""

import time
from dataclasses import dataclass

import httpx

from localagent.models.registry import Provider


@dataclass
class DiscoveredModel:
    """One model reported by an endpoint."""

    id: str
    type: str = "llm"
    loaded: bool | None = None
    context_length: int | None = None
    max_context_length: int | None = None
    tool_use: bool | None = None
    quantization: str = ""
    arch: str = ""
    vision: bool | None = None
    max_output_tokens: int | None = None
    thinking: bool | None = None


_LOADED_TTL = 10.0
_loaded_cache: dict[str, tuple[float, list[DiscoveredModel]]] = {}

# A server that is up answers the TCP handshake in milliseconds, on this computer or across a LAN or
# tailnet; only a server that is off takes long, and Windows spends ~2 s even on a refused loopback
# port. The UI asks several questions of every local server per page load (the model list, what is
# loaded, what `auto` means), so without these a home box that is switched off made it wait ~28 s.
CONNECT_TIMEOUT = 2.0
DOWN_TTL = 15.0  # after a failed connection, answer "unreachable" at once for this long
_down: dict[str, float] = {}


class _Unreachable(Exception):
    """Nothing answered at that address (as opposed to a server that answered with an error)."""


def loaded_models(provider: Provider, timeout: float = 4.0, ttl: float = _LOADED_TTL) -> list[DiscoveredModel]:
    """The models *provider* currently holds in memory, cached for *ttl* seconds.

    Only servers that report load state (LM Studio) can answer; others return
    nothing, which callers read as "unknown", not as "nothing is loaded".
    """
    cached = _loaded_cache.get(provider.name)
    if cached is not None and time.monotonic() - cached[0] < ttl:
        return cached[1]
    found = [m for m in discover(provider, timeout=timeout) if m.loaded]
    _loaded_cache[provider.name] = (time.monotonic(), found)
    return found


def forget_loaded() -> None:
    """Drop the caches (tests, and right after the user changes what is loaded or starts a server)."""
    _loaded_cache.clear()
    _down.clear()


def _root_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


def _get_json(url: str, headers: dict[str, str], timeout: float) -> dict | None:
    """The JSON at *url*, or ``None`` if the server answered without it.

    Raises:
        _Unreachable: nothing answered, so asking the same host for another path is pointless.
    """
    try:
        response = httpx.get(url, headers=headers, timeout=httpx.Timeout(timeout, connect=min(timeout, CONNECT_TIMEOUT)))
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise _Unreachable(str(e) or type(e).__name__) from None
    except httpx.TransportError:
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def is_lmstudio(base_url: str, timeout: float = 5.0) -> bool:
    """Return True when *base_url* answers LM Studio's native ``/api/v0/models``."""
    try:
        data = _get_json(_root_url(base_url) + "/api/v0/models", {}, timeout)
    except _Unreachable:
        return False
    return bool(data and isinstance(data.get("data"), list))


def _discover_gemini(provider: Provider, key: str, timeout: float) -> list[DiscoveredModel] | None:
    """Gemini's native model list, which (unlike its OpenAI-compatible one) reports token limits.

    Listing models does not count against the generation quota.
    """
    base = provider.base_url.rstrip("/")
    root = base[: -len("/openai")] if base.endswith("/openai") else base
    data = _get_json(f"{root}/models?pageSize=1000", {"x-goog-api-key": key}, timeout)
    if data is None:
        return None
    return [
        DiscoveredModel(
            id=m["name"].removeprefix("models/"), type="vlm", context_length=m.get("inputTokenLimit"),
            max_context_length=m.get("inputTokenLimit"), tool_use=True, vision=True,
            max_output_tokens=m.get("outputTokenLimit"), thinking=m.get("thinking"),
        )
        for m in data.get("models", [])
        if "generateContent" in (m.get("supportedGenerationMethods") or [])
    ]


def discover(provider: Provider, timeout: float = 10.0) -> list[DiscoveredModel]:
    """List models on *provider*; raise ``ConnectionError`` if it cannot be reached.

    A provider that could not be reached is not asked again for ``DOWN_TTL`` seconds.
    """
    failed_at = _down.get(provider.base_url)
    if failed_at is not None and time.monotonic() - failed_at < DOWN_TTL:
        raise ConnectionError(f"could not list models at {provider.base_url}: not reachable a moment ago")
    try:
        found = _discover(provider, timeout)
    except _Unreachable as e:
        _down[provider.base_url] = time.monotonic()
        raise ConnectionError(f"could not list models at {provider.base_url}: {e}") from None
    _down.pop(provider.base_url, None)
    return found


def _discover(provider: Provider, timeout: float) -> list[DiscoveredModel]:
    headers = dict(provider.headers)
    key = provider.resolve_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if provider.discovery == "gemini":
        found = _discover_gemini(provider, key, timeout)
        if found is not None:
            return found
    if provider.discovery == "lmstudio":
        data = _get_json(_root_url(provider.base_url) + "/api/v0/models", headers, timeout)
        if data is not None:
            return [
                DiscoveredModel(
                    id=m["id"],
                    type=m.get("type", "llm"),
                    loaded=m.get("state") == "loaded",
                    context_length=m.get("loaded_context_length"),
                    max_context_length=m.get("max_context_length"),
                    tool_use="tool_use" in (m.get("capabilities") or []),
                    quantization=m.get("quantization", ""),
                    arch=m.get("arch", ""),
                    vision=m.get("type") == "vlm",
                )
                for m in data.get("data", [])
            ]
    data = _get_json(provider.base_url.rstrip("/") + "/models", headers, timeout)
    if data is None:
        raise ConnectionError(f"could not list models at {provider.base_url}")
    return [DiscoveredModel(id=str(m["id"]).removeprefix("models/")) for m in data.get("data", [])]
