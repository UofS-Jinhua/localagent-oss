import pytest

from localagent.models.registry import ModelSpec, Provider, Registry, RegistryError, effective_thinking

YAML = """
providers:
  lmstudio:
    base_url: http://localhost:1234/v1
    max_concurrency: 2
    discovery: lmstudio
  cloud:
    base_url: https://api.example.com/v1
    api_key_env: EXAMPLE_KEY
models:
  local:
    provider: lmstudio
    model_id: qwen
    context_length: 122880
    thinking_off: {reasoning_effort: none}
  big:
    provider: cloud
    model_id: big-model
    thinking: always
roles:
  main: {model: local, thinking: on}
  summarizer: {model: local, thinking: off}
  reviewer: big
"""


def load(tmp_path, text=YAML):
    path = tmp_path / "models.yaml"
    path.write_text(text, encoding="utf-8")
    return Registry.load(path)


def test_load_and_resolve_roles(tmp_path):
    registry = load(tmp_path)
    main = registry.resolve("main")
    assert (main.model.model_id, main.provider.name, main.thinking) == ("qwen", "lmstudio", True)
    assert registry.resolve("summarizer").thinking is False
    assert registry.resolve("reviewer").model.name == "big"
    assert registry.resolve("unknown-role").model.name == "local"  # falls back to main
    assert registry.resolve(model="big").thinking is True


def test_round_trip(tmp_path):
    registry = load(tmp_path)
    saved = registry.save(tmp_path / "copy.yaml")
    again = Registry.load(saved)
    assert again.to_dict() == registry.to_dict()


def test_api_key_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMPLE_KEY", "secret")
    assert load(tmp_path).providers["cloud"].resolve_api_key() == "secret"


@pytest.mark.parametrize("bad, message", [
    ("models: {m: {provider: nope, model_id: x}}", "unknown provider"),
    ("providers: {p: {base_url: u}}\nmodels: {m: {provider: p, model_id: x, colour: red}}", "unknown field"),
    ("providers: {p: {base_url: u}}\nroles: {main: ghost}", "unknown model"),
])
def test_invalid_registries_are_rejected(tmp_path, bad, message):
    with pytest.raises(RegistryError, match=message):
        load(tmp_path, bad)


def test_missing_registry_explains_how_to_create_one(tmp_path):
    with pytest.raises(RegistryError, match="la init"):
        Registry.load(tmp_path / "absent.yaml")


def test_effective_thinking():
    toggle = ModelSpec("m", "p", "x")
    assert effective_thinking(toggle, None) is True
    assert effective_thinking(toggle, False) is False
    assert effective_thinking(ModelSpec("m", "p", "x", thinking="never"), True) is False
    assert effective_thinking(ModelSpec("m", "p", "x", thinking="always"), False) is True


def test_keys_come_from_the_environment_or_secrets_file(isolated_home, monkeypatch):
    import os

    from localagent.config import get_secret, read_secrets, save_secret

    save_secret("MY_KEY", "﻿sk-from-file ")  # a PowerShell pipe adds a byte-order mark
    assert read_secrets() == {"MY_KEY": "sk-from-file"} and "MY_KEY" not in os.environ
    provider = Provider(name="p", base_url="https://api.example.com/v1", api_key_env="MY_KEY")
    assert provider.resolve_api_key() == "sk-from-file" and not provider.is_local()
    monkeypatch.setenv("MY_KEY", "sk-from-env")
    assert get_secret("MY_KEY") == "sk-from-env"
    save_secret("MY_KEY", "")
    monkeypatch.delenv("MY_KEY")
    assert provider.resolve_api_key() == ""
    for bad in (("bad name", "x"), ("K", "two\nlines"), ("K", "密钥")):
        with pytest.raises(ValueError):
            save_secret(*bad)


def test_a_thinking_bound_written_in_characters_is_read_as_tokens(tmp_path):
    """Registries from before the bound was counted in tokens keep working, at about where they were."""
    registry = load(tmp_path, "providers: {p: {base_url: u}}\n"
                              "models: {old: {provider: p, model_id: x, max_thinking_chars: 48000},\n"
                              "         unbounded: {provider: p, model_id: y, max_thinking_chars: 0}}")
    assert registry.models["old"].max_thinking_tokens == 13_700
    assert registry.models["unbounded"].max_thinking_tokens == 0
    registry.save()
    assert "max_thinking_chars" not in (tmp_path / "models.yaml").read_text(encoding="utf-8")


def test_vision_and_switchable_survive_a_round_trip(tmp_path):
    registry = Registry.from_dict({
        "providers": {"cloud": {"base_url": "https://x.example/v1", "local": False}},
        "models": {"m": {"provider": "cloud", "model_id": "m", "vision": True, "switchable": True}},
    })
    registry.save(tmp_path / "models.yaml")
    loaded = Registry.load(tmp_path / "models.yaml")
    assert loaded.models["m"].vision and loaded.models["m"].switchable and loaded.providers["cloud"].local is False
    assert [m.name for m in loaded.switch_targets(allow_cloud=False)] == []
    assert [m.name for m in loaded.switch_targets(allow_cloud=True)] == ["m"]
