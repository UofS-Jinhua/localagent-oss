"""Child processes must never pop up a console window.

The desktop window runs under pythonw, which has no console of its own, so on
Windows every child process would open one. The UI polls git every few seconds,
which made console windows flash over the screen; these tests keep that fixed.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from localagent.procutil import NO_WINDOW, new_group, quiet

SOURCE = Path(__file__).resolve().parent.parent / "src" / "localagent"
CALL = re.compile(r"subprocess\.(run|Popen|check_output|call)\(")


def test_a_new_process_group_is_windowless_too():
    group = new_group()
    if sys.platform == "win32":
        assert group["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert group["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert group == {"start_new_session": True}


def test_quiet_hides_the_window_only_on_windows():
    flags = quiet()
    if sys.platform == "win32":
        assert flags["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert quiet(subprocess.CREATE_NEW_PROCESS_GROUP)["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert flags == {} and NO_WINDOW == 0
    assert quiet(cwd="/tmp")["cwd"] == "/tmp"


@pytest.mark.parametrize("path", sorted(SOURCE.rglob("*.py")), ids=lambda p: p.name)
def test_every_subprocess_call_passes_the_flags(path):
    """A new call site must use quiet(), or it will flash a console window in the desktop app."""
    if path.name == "procutil.py":
        return
    text = path.read_text(encoding="utf-8")
    for match in CALL.finditer(text):
        depth, end = 0, match.end() - 1  # start at the call's own "("
        for index in range(match.end() - 1, min(len(text), match.end() + 1500)):
            depth += (text[index] == "(") - (text[index] == ")")
            if depth == 0:
                end = index
                break
        arguments = text[match.start():end]
        # new_group() is quiet() plus a process group of its own (see procutil).
        assert "quiet(" in arguments or "new_group(" in arguments,             f"{path.name}: {arguments.splitlines()[0]}… needs **quiet() or **new_group()"
