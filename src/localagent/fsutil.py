"""Filesystem helpers."""

import os
import shutil
import stat
import sys
from pathlib import Path


def _make_writable_and_retry(function, path, _error) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _literal(path: Path) -> str:
    """On Windows the ``\\\\?\\`` form, in which a file named ``nul`` or ``con`` is a file and not a device."""
    text = str(path.resolve())
    return "\\\\?\\" + text if sys.platform == "win32" and not text.startswith("\\\\") else text


def remove_tree(path: Path) -> bool:
    """Delete a directory tree, including read-only files, and report whether it is gone.

    Git marks its object files read-only, which makes a plain ``shutil.rmtree``
    fail on Windows; ``ignore_errors=True`` would hide that and leave the tree behind.
    A file named like a device (``nul``, left by a command's ``2>nul`` in Git Bash)
    cannot be deleted through an ordinary path, so the tree is removed through the
    literal one.
    """
    if not path.exists():
        return True
    try:
        shutil.rmtree(_literal(path), onexc=_make_writable_and_retry)
    except OSError:
        pass
    return not path.exists()
