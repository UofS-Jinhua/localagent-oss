"""Helpers of a task stay on the task's machine.

The setup this guards: a home LM Studio and a GPU server behind an ssh tunnel, both
local, every role left at `auto`. A task the user runs on the GPU server used to send
its sub-agent, reviewer and commit message to whichever server `auto` found first --
the home box, a different machine -- or fail outright when only the GPU server was up.
"""

import subprocess

import pytest
from scripted_server import ScriptedLLM

from localagent.agent import ChatSession
from localagent.models.discovery import forget_loaded
from localagent.models.registry import ModelSpec, Provider, Registry, RoleSpec
from localagent.models.sync import forget_sync
from localagent.store import Store

HOME = {"id": "home-27b", "type": "llm", "state": "loaded", "loaded_context_length": 120000,
        "max_context_length": 262144, "capabilities": ["tool_use"]}


@pytest.fixture(autouse=True)
def fresh_caches():
    forget_loaded()
    forget_sync()
    yield
    forget_loaded()
    forget_sync()


@pytest.fixture
def home():
    """An LM Studio at home: it reports a loaded model, so plain `auto` finds it first."""
    with ScriptedLLM(lmstudio=True, models=[dict(HOME)]) as server:
        yield server


@pytest.fixture
def gpu():
    """An on-demand server (llama-swap style) behind a tunnel: it lists models but not load state."""
    with ScriptedLLM(models=[{"id": "gpu-27b"}]) as server:
        yield server


def two_servers(home, gpu, **roles) -> Registry:
    return Registry(
        providers={
            "home": Provider(name="home", base_url=home.base_url, discovery="lmstudio"),
            "gpu": Provider(name="gpu", base_url=gpu.base_url),
            "cloud": Provider(name="cloud", base_url="https://api.example.com/v1", local=False),
        },
        models={
            "home-27b": ModelSpec(name="home-27b", provider="home", model_id="home-27b", context_length=120000),
            "gpu-27b": ModelSpec(name="gpu-27b", provider="gpu", model_id="gpu-27b", context_length=120000),
            "cloud-x": ModelSpec(name="cloud-x", provider="cloud", model_id="cloud-x", context_length=120000),
        },
        roles={name: RoleSpec(model=model, thinking=thinking)
               for name, (model, thinking) in {"main": ("auto", True), "subagent": ("auto", True),
                                               "commit_message": ("auto", False), **roles}.items()},
    )


def test_a_helper_left_at_auto_follows_a_local_task(home, gpu):
    registry = two_servers(home, gpu)
    assert registry.helper("subagent", "gpu-27b") == ("gpu-27b", True)
    assert registry.helper("commit_message", "gpu-27b") == ("gpu-27b", False)  # the role's thinking stays
    assert registry.helper("reviewer", "gpu-27b") == ("gpu-27b", True)  # unset: main's settings


def test_what_the_user_chose_for_a_role_is_obeyed(home, gpu):
    named = two_servers(home, gpu, reviewer=("cloud-x", None))  # a cross-vendor review on purpose
    assert named.helper("reviewer", "gpu-27b") == (None, None)
    scoped = two_servers(home, gpu, subagent=("auto:home", True))  # "whatever home has loaded"
    assert scoped.helper("subagent", "gpu-27b") == (None, True)


def test_an_unset_role_follows_the_task_even_when_main_is_pinned(home, gpu):
    """main pinned to the GPU server is the default; a task moved to the home box keeps its helpers there."""
    registry = two_servers(home, gpu, main=("gpu-27b", True))
    assert registry.helper("reviewer", "home-27b") == ("home-27b", True)  # no reviewer role: follow the task
    assert registry.helper("subagent", "home-27b") == ("home-27b", True)
    assert registry.helper("reviewer", "cloud-x") == (None, True)  # a cloud task: fall back to the pinned main


def test_a_cloud_task_does_not_make_helper_work_paid(home, gpu):
    assert two_servers(home, gpu).helper("commit_message", "cloud-x") == (None, False)


def test_a_task_on_the_gpu_server_never_touches_the_home_box(home, gpu, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)

    gpu.add_tool_calls([("Task", {"instructions": "Say where add() is defined."})])
    gpu.add_tool_calls([("finish", {"success": True, "summary": "app.py line 1"})])  # the sub-agent
    gpu.add_tool_calls([("Write", {"file_path": "notes.md", "content": "add() is in app.py\n"})])
    gpu.add_tool_calls([("finish", {"success": True, "summary": "wrote notes.md"})])
    gpu.add_text("docs: note where add() lives")  # the commit message

    store = Store(tmp_path / "helpers.db")
    result = ChatSession(two_servers(home, gpu), repo, store=store, model="gpu-27b").run("note where add() is")
    store.close()

    assert result.success
    assert home.requests == []  # neither the sub-agent nor the commit message went to the other machine
    assert len(gpu.requests) == 5
    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).stdout.strip()

    [branch] = git("for-each-ref", "--format=%(refname:short)", "refs/heads/la/").splitlines()
    assert git("log", "-1", "--format=%s", branch) == "docs: note where add() lives"
