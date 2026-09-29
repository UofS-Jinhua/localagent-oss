"""Console stream setup shared by the CLI and the scripts.

Two Windows problems, one place to solve them:

* started from a shortcut with ``pythonw`` there is no console at all and both
  streams are ``None``, so the first print would end the process;
* redirected to a file, Python picks the system code page (GBK on this
  machine), and the first box-drawing character raises UnicodeEncodeError.
"""

import sys
from pathlib import Path

from localagent.config import home_dir


def prepare_streams(log_name: str = "ui.log") -> None:
    """Make stdout/stderr exist and accept any text. Safe to call more than once."""
    if sys.stdout is None or sys.stderr is None:
        path = home_dir() / log_name
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("a", encoding="utf-8", errors="replace", buffering=1)  # closed with the process
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # already detached, or not a real file
                pass


def log_path(name: str = "ui.log") -> Path:
    return home_dir() / name
