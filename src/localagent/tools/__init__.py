"""The agent's tools."""

import threading

from localagent.tools.ask import Asker, make_ask_tool
from localagent.tools.base import Tool, ToolError
from localagent.tools.fs import make_fs_tools
from localagent.tools.search import make_search_tools
from localagent.tools.shell import Approver, OutputCallback, make_bash_tool
from localagent.tools.testing import make_test_tool
from localagent.tools.web import WebSettings, make_web_tools
from localagent.tools.workspace import Workspace


def make_tools(
    ws: Workspace,
    approver: Approver | None = None,
    on_output: OutputCallback | None = None,
    stop_event: threading.Event | None = None,
    asker: Asker | None = None,
    read_only: bool = False,
    web: WebSettings | None = None,
) -> list[Tool]:
    """Return the standard toolset: Read, Write, Edit, Bash, RunTests, Grep, Glob.

    AskUser joins them only when *asker* is given, so an unattended run never
    offers the model a question nobody would answer. With *read_only* the tools
    that change files are left out (the reviewer reads and runs tests), and
    RunTests loses its ``command`` argument, which would otherwise be a shell.
    WebSearch and WebFetch join when *web* settings are given and enabled; they
    change nothing on disk, so read-only agents get them too.
    """
    tools = [
        *make_fs_tools(ws),
        make_bash_tool(ws, approver, on_output, stop_event),
        make_test_tool(ws, approver, on_output, stop_event, read_only),
        *make_search_tools(ws),
        *make_web_tools(web),
    ]
    if read_only:
        tools = [t for t in tools if t.name not in ("Write", "Edit", "Bash")]
    if asker is not None:
        tools.append(make_ask_tool(asker))
    return tools


__all__ = ["Asker", "Tool", "ToolError", "WebSettings", "Workspace", "make_tools"]
