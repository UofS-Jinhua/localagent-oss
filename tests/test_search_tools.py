import subprocess

import pytest

from localagent.tools import ToolError, Workspace
from localagent.tools.search import make_search_tools


@pytest.fixture
def tools(project):
    (project / "src").mkdir()
    (project / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (project / "src" / "util.py").write_text("# helper\nADD = 'add'\n", encoding="utf-8")
    (project / "README.md").write_text("Use add() to add.\n", encoding="utf-8")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "junk.py").write_text("def add(): pass\n", encoding="utf-8")
    return {t.name: t for t in make_search_tools(Workspace(project))}


def test_grep_finds_matches_with_line_numbers(tools):
    out = tools["Grep"].call({"pattern": r"def add"})
    assert "src/calc.py:1: def add(a, b):" in out
    assert "node_modules" not in out


def test_grep_glob_and_ignore_case(tools):
    out = tools["Grep"].call({"pattern": "ADD", "glob": "*.py", "ignore_case": True})
    assert "src/util.py" in out and "README.md" not in out


def test_grep_invalid_regex(tools):
    with pytest.raises(ToolError, match="invalid regular expression"):
        tools["Grep"].call({"pattern": "("})


def test_grep_caps_results(tools):
    out = tools["Grep"].call({"pattern": "add", "max_results": 1})
    assert "more matches not shown" in out


def test_glob_matches_names_at_any_depth(tools):
    out = tools["Glob"].call({"pattern": "*.py"})
    assert out.splitlines() == ["src/calc.py", "src/util.py"]


def test_glob_respects_gitignore(project, tools):
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / ".gitignore").write_text("src/util.py\n", encoding="utf-8")
    assert "src/util.py" not in tools["Glob"].call({"pattern": "*.py"})
