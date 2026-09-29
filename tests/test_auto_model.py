"""The `auto` role target: follow whatever the local server has loaded."""

import pytest
from scripted_server import ScriptedLLM

from localagent.models.discovery import forget_loaded
from localagent.models.registry import ModelSpec, Provider, Registry, RegistryError, RoleSpec

BIG = {"id": "big-27b", "type": "vlm", "state": "loaded", "loaded_context_length": 122880,
       "max_context_length": 262144, "capabilities": ["tool_use"], "quantization": "IQ4_XS", "arch": "qwen"}
SMALL = {"id": "small-9b", "type": "llm", "state": "not-loaded", "max_context_length": 32768,
         "capabilities": ["tool_use"], "quantization": "Q4", "arch": "qwen"}


@pytest.fixture
def server():
    forget_loaded()
    with ScriptedLLM(lmstudio=True, models=[dict(BIG), dict(SMALL)]) as s:
        yield s
    forget_loaded()


def build(server, models=None, role_model="auto") -> Registry:
    provider = Provider(name="lmstudio", base_url=server.base_url, discovery="lmstudio")
    return Registry(providers={"lmstudio": provider}, models=models or {},
                    roles={"main": RoleSpec(model=role_model, thinking=True)})


def registered(server, **overrides) -> dict[str, ModelSpec]:
    spec = ModelSpec(name="big-27b", provider="lmstudio", model_id="big-27b", context_length=32768, **overrides)
    return {spec.name: spec}


def test_auto_picks_the_loaded_model(server):
    resolved = build(server, registered(server)).resolve()
    assert resolved.model.name == "big-27b"
    # The server knows the loaded context and vision better than the registry entry does.
    assert resolved.model.context_length == 122880 and resolved.model.vision


def test_auto_follows_the_server_when_it_changes(server):
    registry = build(server, registered(server))
    assert registry.resolve().model.name == "big-27b"
    forget_loaded()
    server.models = [{**BIG, "state": "not-loaded"}, {**SMALL, "state": "loaded", "loaded_context_length": 32768}]
    assert registry.resolve().model.name == "small-9b"  # not registered, but usable right away


def test_auto_says_what_is_wrong(server):
    forget_loaded()
    server.models = [{**BIG, "state": "not-loaded"}]
    with pytest.raises(RegistryError, match="no model loaded"):
        build(server, registered(server)).resolve()

    named = build(server, registered(server), role_model="auto:nope")
    assert named.problems() == ["role 'main': 'auto:nope' names an unknown provider"]
    with pytest.raises(RegistryError, match="not in the registry"):
        named.resolve()


def test_auto_survives_a_round_trip_and_validation(server, tmp_path):
    registry = build(server, registered(server))
    registry.save(tmp_path / "models.yaml")
    loaded = Registry.load(tmp_path / "models.yaml")
    assert loaded.roles["main"].model == "auto" and loaded.problems() == []
    assert loaded.resolve().model.name == "big-27b"


def test_an_unreachable_server_is_reported_clearly(server):
    registry = build(server, registered(server))
    server.__exit__()
    forget_loaded()
    with pytest.raises(RegistryError, match="could not list models"):
        registry.resolve()
