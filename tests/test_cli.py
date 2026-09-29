import sys
import subprocess

from scripted_server import ScriptedLLM

from localagent.cli.main import main
from localagent.models.registry import Registry
from localagent.store import Store


def test_init_registers_lmstudio_models(isolated_home):
    models = [
        {"id": "qwen-27b", "type": "llm", "state": "loaded", "loaded_context_length": 122880,
         "max_context_length": 262144, "capabilities": ["tool_use"], "quantization": "IQ4_XS", "arch": "qwen35"},
        {"id": "small-9b", "type": "vlm", "state": "not-loaded", "max_context_length": 262144},
        {"id": "text-embedding-nomic", "type": "embeddings", "state": "not-loaded"},
    ]
    with ScriptedLLM(lmstudio=True, models=models) as server:
        assert main(["init", "--base-url", server.base_url.removesuffix("/v1")]) == 0
    registry = Registry.load()
    assert registry.providers["lmstudio"].discovery == "lmstudio"
    assert set(registry.models) == {"qwen-27b", "small-9b"}
    assert registry.models["qwen-27b"].context_length == 122880
    assert registry.models["small-9b"].context_length == 32768
    assert registry.models["qwen-27b"].thinking_off == {"reasoning_effort": "none"}
    # A server that reports what it has loaded gets "auto": the role follows it.
    assert registry.roles["main"].model == "auto"
    assert registry.resolve().model.name == "qwen-27b"
    assert registry.roles["summarizer"].thinking is False


def test_init_refuses_duplicate_provider(isolated_home):
    with ScriptedLLM(lmstudio=True) as server:
        assert main(["init", "--base-url", server.base_url]) == 0
        assert main(["init", "--base-url", server.base_url]) == 1
        assert main(["init", "--base-url", server.base_url, "--provider", "second"]) == 0
    assert set(Registry.load().providers) == {"lmstudio", "second"}


def test_run_executes_a_task(isolated_home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    with ScriptedLLM(lmstudio=True) as server:
        assert main(["init", "--base-url", server.base_url]) == 0
        server.add_tool_calls([("Write", {"file_path": "hello.txt", "content": "hi\n"})])
        server.add_tool_calls([("finish", {"success": True, "summary": "created hello.txt"})])
        assert main(["run", "-w", str(work), "create hello.txt"]) == 0
    assert (work / "hello.txt").read_text(encoding="utf-8") == "hi\n"


def test_continue_chat_and_inspect_it(isolated_home, tmp_path, capsys):
    work = tmp_path / "work"
    work.mkdir()
    with ScriptedLLM(lmstudio=True) as server:
        assert main(["init", "--base-url", server.base_url]) == 0
        server.add_tool_calls([("finish", {"success": True, "summary": "picked the name Nova"})])
        assert main(["run", "-w", str(work), "pick a project name"]) == 0
        server.add_tool_calls([("finish", {"success": True, "summary": "wrote the tagline"})])
        assert main(["run", "-w", str(work), "-c", "write a tagline for it"]) == 0
        assert "picked the name Nova" in server.requests[1]["messages"][1]["content"]
        assert main(["run", "-w", str(tmp_path), "-c", "nothing to continue"]) == 1
    capsys.readouterr()
    chat_id = Store().latest_chat(work)
    assert main(["chats", "-w", str(work)]) == 0
    assert chat_id in capsys.readouterr().out
    assert main(["show", chat_id[:8]]) == 0
    shown = capsys.readouterr().out
    assert "pick a project name" in shown and "wrote the tagline" in shown


def test_branch_workflow(isolated_home, tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "T")):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    with ScriptedLLM(lmstudio=True) as server:
        assert main(["init", "--base-url", server.base_url]) == 0
        server.add_tool_calls([("Write", {"file_path": "b.py", "content": "y = 2\n"})])
        server.add_tool_calls([("finish", {"success": True, "summary": "added b.py"})])
        assert main(["run", "-w", str(repo), "--plain-commits", "add b.py"]) == 0
        capsys.readouterr()
        assert main(["status", "-w", str(repo)]) == 0
        status = capsys.readouterr().out
        assert "current branch: main" in status and "la/" in status
        assert main(["diff", "-w", str(repo), "--stat"]) == 0
        assert "b.py" in capsys.readouterr().out
        assert main(["merge", "-w", str(repo), "--plain"]) == 0
        assert "Merged" in capsys.readouterr().out
    assert (repo / "b.py").exists()
    log = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=repo, capture_output=True, text=True).stdout
    assert log.startswith("la: add b.py") and "Tasks:\n- add b.py" in log
    assert main(["status", "-w", str(repo)]) == 0
    assert "no pending chat branches" in capsys.readouterr().out


def test_commands_without_registry_explain_setup(isolated_home, capsys):
    assert main(["models", "list"]) == 1
    assert "la init" in capsys.readouterr().out


def test_a_console_less_launch_keeps_working(isolated_home, monkeypatch):
    """Started from a shortcut (pythonw) there is no console: printing must not kill the process."""
    from localagent.cli.output import prepare_streams

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    prepare_streams()
    assert sys.stdout is not None and sys.stderr is not None
    print("hello from the window")
    sys.stdout.flush()
    assert "hello from the window" in (isolated_home / "ui.log").read_text(encoding="utf-8")


def test_redirected_output_takes_any_character(tmp_path, monkeypatch):
    """Redirected to a file, Windows picks the local code page; box drawing must still print."""
    from localagent.cli.output import prepare_streams

    path = tmp_path / "out.txt"
    with path.open("w", encoding="gbk") as handle:  # what Python picks on this machine
        monkeypatch.setattr(sys, "stdout", handle)
        prepare_streams()
        print("▶ 结果 ✔")
        sys.stdout.flush()
    assert "▶" in path.read_text(encoding="utf-8")


def test_show_reports_what_each_task_cost(isolated_home, project, llm):
    """A local window is small and slow to refill: the token bill has to be visible."""
    store = Store()
    chat = store.new_chat(project, title="numbers")
    task = store.add_task(chat, "do something", "qwen", project)
    store.finish_task(task, success=True, result="did it", reason="finished", steps=4,
                      sessions=1, total_tokens=12345, elapsed=42.0, cost=0.0)
    store.close()
    assert main(["show", chat]) == 0
