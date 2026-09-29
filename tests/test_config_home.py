"""Where LocalAgent keeps its own files, and how an older ~/.localagent moves in."""

import pytest

from localagent import config


@pytest.fixture
def no_override(monkeypatch):
    monkeypatch.delenv("LOCALAGENT_HOME", raising=False)
    monkeypatch.setattr(config, "_tried_migration", False)


def test_the_checkout_keeps_its_own_data_folder(no_override, tmp_path, monkeypatch):
    data = tmp_path / "checkout" / "data"
    monkeypatch.setattr(config, "_checkout_data_dir", lambda: data)
    monkeypatch.setattr(config, "LEGACY_HOME", tmp_path / "nothing-here")

    assert config.home_dir() == data
    assert config.registry_path() == data / "models.yaml"


def test_an_older_home_moves_into_the_checkout_whole(no_override, tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    (legacy / "attachments").mkdir(parents=True)
    (legacy / "models.yaml").write_text("models: {}\n", encoding="utf-8")
    (legacy / "secrets.env").write_text("K=v\n", encoding="utf-8")
    data = tmp_path / "checkout" / "data"
    monkeypatch.setattr(config, "_checkout_data_dir", lambda: data)
    monkeypatch.setattr(config, "LEGACY_HOME", legacy)

    assert config.home_dir() == data
    assert (data / "models.yaml").read_text(encoding="utf-8") == "models: {}\n"
    assert (data / "attachments").is_dir() and not legacy.exists()


def test_an_older_home_that_cannot_be_moved_keeps_being_used(no_override, tmp_path, monkeypatch):
    """All or nothing: a half-moved installation would lose keys or history."""
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "models.yaml").write_text("models: {}\n", encoding="utf-8")
    data = tmp_path / "checkout" / "data"
    monkeypatch.setattr(config, "_checkout_data_dir", lambda: data)
    monkeypatch.setattr(config, "LEGACY_HOME", legacy)
    monkeypatch.setattr(config.Path, "rename", lambda *args: (_ for _ in ()).throw(OSError("in use")))

    assert config.home_dir() == legacy
    assert (legacy / "models.yaml").is_file() and not data.exists()


def test_the_environment_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAGENT_HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(config, "_checkout_data_dir", lambda: tmp_path / "data")

    assert config.home_dir() == tmp_path / "elsewhere"


def test_a_wheel_install_stays_in_the_user_home(no_override, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_checkout_data_dir", lambda: None)
    monkeypatch.setattr(config, "LEGACY_HOME", tmp_path / "user-home")

    assert config.home_dir() == tmp_path / "user-home"


def test_the_source_tree_is_recognised_by_its_pyproject():
    """The real checkout: this test runs from it, so data/ is where the files belong."""
    found = config._checkout_data_dir()
    assert found is not None and found.name == "data" and (found.parent / "pyproject.toml").is_file()


def test_a_chats_worktree_uses_the_checkouts_data(no_override, tmp_path, monkeypatch):
    """LocalAgent working on itself: the copy under .la-worktrees must not get an empty registry."""
    worktree = tmp_path / "repo" / ".la-worktrees" / "abc123"
    (worktree / "src" / "localagent").mkdir(parents=True)
    (worktree / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr(config, "__file__", str(worktree / "src" / "localagent" / "config.py"))

    assert config._checkout_data_dir() == tmp_path / "repo" / "data"
