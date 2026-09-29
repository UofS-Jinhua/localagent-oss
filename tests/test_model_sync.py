"""The registry follows the local server: models downloaded there appear, deleted ones go."""

import pytest
from scripted_server import ScriptedLLM

from localagent.models.discovery import forget_loaded
from localagent.models.registry import ModelSpec, Provider, Registry, RoleSpec
from localagent.models.sync import forget_sync, loaded_now, sync_local_models

BIG = {"id": "big-27b", "type": "vlm", "state": "loaded", "loaded_context_length": 122880,
       "max_context_length": 262144, "capabilities": ["tool_use"], "quantization": "IQ4_XS", "arch": "qwen"}
SMALL = {"id": "small-9b", "type": "llm", "state": "not-loaded", "max_context_length": 32768,
         "capabilities": ["tool_use"], "quantization": "Q4", "arch": "qwen"}
EMBED = {"id": "nomic-embed-text", "type": "embeddings", "state": "not-loaded", "max_context_length": 2048}


@pytest.fixture(autouse=True)
def fresh_caches():
    forget_sync()
    forget_loaded()
    yield
    forget_sync()
    forget_loaded()


@pytest.fixture
def server():
    with ScriptedLLM(lmstudio=True, models=[dict(BIG), dict(SMALL), dict(EMBED)]) as s:
        yield s


def build(server, models=None, path=None, roles=None) -> Registry:
    provider = Provider(name="lmstudio", base_url=server.base_url, discovery="lmstudio")
    registry = Registry(providers={"lmstudio": provider}, models=models or {},
                        roles=roles or {"main": RoleSpec(model="auto", thinking=True)})
    registry.path = path
    return registry


def spec(name: str, **overrides) -> ModelSpec:
    return ModelSpec(name=name, provider="lmstudio", model_id=name, context_length=32768, **overrides)


def test_models_on_the_server_are_added_and_the_rest_dropped(server, tmp_path):
    registry = build(server, {"gone-13b": spec("gone-13b")}, path=tmp_path / "models.yaml")

    result = sync_local_models(registry)

    assert set(registry.models) == {"big-27b", "small-9b"}  # the embedding model is not a chat model
    assert result.added == ["big-27b", "small-9b"] and result.removed == ["gone-13b"]
    assert registry.models["big-27b"].context_length == 122880 and registry.models["big-27b"].vision
    assert (tmp_path / "models.yaml").is_file()  # written back, so the next process sees it too


def test_a_model_a_role_points_at_is_kept(server, tmp_path):
    registry = build(server, {"gone-13b": spec("gone-13b")}, path=tmp_path / "models.yaml",
                     roles={"main": RoleSpec(model="gone-13b", thinking=True)})

    result = sync_local_models(registry)

    assert "gone-13b" in registry.models and result.kept == ["gone-13b"]
    assert not result.removed


def test_an_unreachable_server_changes_nothing(tmp_path):
    provider = Provider(name="lmstudio", base_url="http://127.0.0.1:9/v1", discovery="lmstudio")
    registry = Registry(providers={"lmstudio": provider}, models={"kept-7b": spec("kept-7b")},
                        roles={"main": RoleSpec(model="auto", thinking=True)})
    registry.path = tmp_path / "models.yaml"

    result = sync_local_models(registry, timeout=0.5)

    assert set(registry.models) == {"kept-7b"} and result.unreachable == ["lmstudio"]
    assert not (tmp_path / "models.yaml").exists()


def test_cloud_models_are_left_alone(server, tmp_path):
    cloud = Provider(name="cloud", base_url="https://api.example.com/v1", local=False)
    registry = build(server, {"gpt-x": ModelSpec(name="gpt-x", provider="cloud", model_id="gpt-x")},
                     path=tmp_path / "models.yaml")
    registry.providers["cloud"] = cloud

    sync_local_models(registry)

    assert "gpt-x" in registry.models  # listing a cloud catalogue costs a request and lists hundreds


def test_a_named_model_gets_the_window_it_is_actually_loaded_with(server, tmp_path):
    """The registry records what the model looked like when it was added; the server has the truth."""
    registry = build(server, {"big-27b": spec("big-27b", vision=False)}, path=tmp_path / "models.yaml",
                     roles={"main": RoleSpec(model="big-27b", thinking=True)})

    resolved = registry.resolve()

    assert resolved.model.context_length == 122880 and resolved.model.vision
    assert registry.models["big-27b"].context_length == 32768  # the stored entry is not rewritten


def test_a_model_that_is_not_loaded_keeps_its_stored_window(server, tmp_path):
    registry = build(server, {"small-9b": spec("small-9b")}, path=tmp_path / "models.yaml",
                     roles={"main": RoleSpec(model="small-9b", thinking=True)})

    assert registry.resolve().model.context_length == 32768


def test_load_state_is_read_live_not_stored(server, tmp_path):
    registry = build(server, path=tmp_path / "models.yaml")
    sync_local_models(registry)

    assert "loaded" not in registry.models["big-27b"].notes  # it would go stale in models.yaml
    assert set(loaded_now(registry)) == {("lmstudio", "big-27b")}

    forget_loaded()
    server.models = [{**BIG, "state": "not-loaded"}, {**SMALL, "state": "loaded", "loaded_context_length": 32768}]
    assert set(loaded_now(registry)) == {("lmstudio", "small-9b")}


def test_a_server_that_is_off_costs_one_short_wait_not_one_per_question():
    """The page asks every local server several things; a switched-off home box made it wait ~28 s."""
    import time

    from localagent.models.discovery import CONNECT_TIMEOUT, discover, loaded_models

    provider = Provider(name="off", base_url="http://192.0.2.1:1234/v1", discovery="lmstudio")  # TEST-NET: nobody
    started = time.monotonic()
    with pytest.raises(ConnectionError, match="could not list models"):
        discover(provider, timeout=10)
    assert time.monotonic() - started < CONNECT_TIMEOUT + 1.5  # one short attempt, not 10 s, not a second path

    started = time.monotonic()
    with pytest.raises(ConnectionError):
        loaded_models(provider)
    with pytest.raises(ConnectionError):
        discover(provider)
    assert time.monotonic() - started < 0.5  # remembered as down for a few seconds
