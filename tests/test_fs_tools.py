import locale

import pytest

from localagent.tools import ToolError, Workspace
from localagent.tools.fs import make_fs_tools


@pytest.fixture
def tools(project):
    ws = Workspace(project)
    return ws, {t.name: t for t in make_fs_tools(ws)}


def test_read_numbers_lines_and_windows(project, tools):
    _, t = tools
    (project / "a.py").write_text("".join(f"line {i}\n" for i in range(1, 11)), encoding="utf-8")
    out = t["Read"].call({"file_path": "a.py", "start_line": 3, "max_lines": 2})
    assert out.splitlines()[0] == "a.py (lines 3-4 of 10)"
    assert "     3\tline 3" in out and "     4\tline 4" in out and "line 5" not in out.split("[")[0]
    assert "continue with start_line=5" in out


def test_read_directory_lists_entries(project, tools):
    _, t = tools
    (project / "pkg").mkdir()
    (project / "pkg" / "m.py").write_text("x = 1\n", encoding="utf-8")
    assert "m.py" in t["Read"].call({"file_path": "pkg"})


def test_read_missing_file_suggests_similar_name(project, tools):
    _, t = tools
    (project / "calculator.py").write_text("", encoding="utf-8")
    with pytest.raises(ToolError, match="calculator.py"):
        t["Read"].call({"file_path": "calculater.py"})


def _code_page_reads_gbk() -> bool:
    try:
        "中文".encode("gbk").decode(locale.getpreferredencoding(False))
    except (UnicodeDecodeError, LookupError):
        return False
    return True


# decode() tries UTF-8, then the system code page: GBK files read as Chinese only where that
# code page is GBK (Chinese Windows). On a UTF-8 system they fall back to Latin-1.
@pytest.mark.skipif(not _code_page_reads_gbk(), reason="the system code page is not GBK")
def test_read_gbk_file(project, tools):
    _, t = tools
    (project / "zh.txt").write_bytes("中文内容\n".encode("gbk"))
    assert "中文内容" in t["Read"].call({"file_path": "zh.txt"})


def test_edit_requires_read_first(project, tools):
    _, t = tools
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ToolError, match="Read it before"):
        t["Edit"].call({"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"})


def test_edit_replaces_unique_string_and_shows_snippet(project, tools):
    _, t = tools
    (project / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    t["Read"].call({"file_path": "a.py"})
    out = t["Edit"].call({"file_path": "a.py", "old_string": "return 1", "new_string": "return 2"})
    assert (project / "a.py").read_text(encoding="utf-8") == "def f():\n    return 2\n"
    assert "return 2" in out


def test_edit_rejects_ambiguous_match_unless_replace_all(project, tools):
    _, t = tools
    (project / "a.py").write_text("a = 0\nb = 0\n", encoding="utf-8")
    t["Read"].call({"file_path": "a.py"})
    with pytest.raises(ToolError, match="occurs 2 times"):
        t["Edit"].call({"file_path": "a.py", "old_string": "= 0", "new_string": "= 1"})
    t["Edit"].call({"file_path": "a.py", "old_string": "= 0", "new_string": "= 1", "replace_all": "true"})
    assert (project / "a.py").read_text(encoding="utf-8") == "a = 1\nb = 1\n"


def test_edit_detects_external_change(project, tools):
    _, t = tools
    path = project / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")
    t["Read"].call({"file_path": "a.py"})
    path.write_text("x = 5\n", encoding="utf-8")
    with pytest.raises(ToolError, match="changed on disk"):
        t["Edit"].call({"file_path": "a.py", "old_string": "x = 5", "new_string": "x = 6"})


def test_edit_strips_pasted_line_number_prefixes(project, tools):
    _, t = tools
    (project / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    t["Read"].call({"file_path": "a.py"})
    out = t["Edit"].call({"file_path": "a.py", "old_string": "     2\t    return 1",
                          "new_string": "     2\t    return 3"})
    assert "line-number prefixes" in out
    assert (project / "a.py").read_text(encoding="utf-8") == "def f():\n    return 3\n"


def test_edit_not_found_shows_closest_lines(project, tools):
    _, t = tools
    (project / "a.py").write_text("def total(xs):\n    return sum(xs) / len(xs)\n", encoding="utf-8")
    t["Read"].call({"file_path": "a.py"})
    with pytest.raises(ToolError, match="most similar text is at lines 2-2"):
        t["Edit"].call({"file_path": "a.py", "old_string": "return sum(xs) / len(x)", "new_string": "return 0"})


def test_edit_preserves_crlf(project, tools):
    _, t = tools
    (project / "a.txt").write_bytes(b"one\r\ntwo\r\n")
    t["Read"].call({"file_path": "a.txt"})
    t["Edit"].call({"file_path": "a.txt", "old_string": "one\ntwo", "new_string": "uno\ndos"})
    assert (project / "a.txt").read_bytes() == b"uno\r\ndos\r\n"


def test_write_new_file_but_not_unread_existing(project, tools):
    _, t = tools
    t["Write"].call({"file_path": "new/b.py", "content": "print('hi')\n"})
    assert (project / "new" / "b.py").read_text(encoding="utf-8") == "print('hi')\n"
    (project / "c.py").write_text("old\n", encoding="utf-8")
    with pytest.raises(ToolError, match="already exists"):
        t["Write"].call({"file_path": "c.py", "content": "new\n"})


def test_write_outside_workspace_is_refused(project, tools, tmp_path):
    _, t = tools
    with pytest.raises(ToolError, match="outside the working directory"):
        t["Write"].call({"file_path": str(tmp_path / "escape.txt"), "content": "x"})


def test_missing_argument_names_signature(tools):
    _, t = tools
    with pytest.raises(ToolError, match=r"Edit\(file_path, old_string, new_string, replace_all=\?\)"):
        t["Edit"].call({"file_path": "a.py"})
