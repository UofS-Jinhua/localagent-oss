"""``la ui``: serve the UI locally and show it in a desktop window (pywebview) or a browser."""

import socket
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

import uvicorn

from localagent.server.app import create_app

# The repository's icon (the shortcut uses it too); absent when the package is installed some other way.
ICON = Path(__file__).resolve().parents[3] / "assets" / "localagent.ico"


def free_port(host: str) -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


class DesktopBridge:
    """Methods the page can call as ``window.pywebview.api.<name>()`` inside the desktop window."""

    def pick_folder(self) -> str | None:
        import webview

        chosen = webview.windows[0].create_file_dialog(webview.FileDialog.FOLDER)
        return chosen[0] if chosen else None


def start_server(host: str, port: int, token: str, desktop: bool = False) -> tuple[uvicorn.Server, threading.Thread]:
    """Run the server in a background thread and wait until it accepts connections."""
    server = uvicorn.Server(uvicorn.Config(create_app(token, desktop=desktop), host=host, port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, name="localagent-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError(f"the server did not start on {host}:{port}")
        time.sleep(0.05)
    return server, thread


def run_ui(work_dir: str | None = None, browser: bool = False, host: str = "127.0.0.1", port: int = 0,
           debug: bool = False, open_browser: bool = True) -> int:
    """Start the server and open the UI; returns when the window closes (or on Ctrl+C in browser mode).

    Without *work_dir* the page opens the folder used last (the server keeps the recent list: the
    window's port, and with it the page's local storage, is new on every start).
    """
    import secrets

    token = secrets.token_urlsafe(24)
    port = port or free_port(host)
    server, thread = start_server(host, port, token, desktop=not browser)
    url = f"http://{host}:{port}/#token={token}"
    if work_dir:
        url += f"&dir={quote(str(Path(work_dir).resolve()))}"
    try:
        if browser:
            print(f"LocalAgent UI: {url}\nPress Ctrl+C to stop.", flush=True)
            if open_browser:
                webbrowser.open(url)
            while thread.is_alive():
                thread.join(0.5)
        else:
            import webview

            # text_select: pywebview turns selection off by default, and then nothing on the page can be copied.
            webview.create_window("LocalAgent", url, js_api=DesktopBridge(), width=1440, height=920,
                                  min_size=(960, 620), text_select=True)
            webview.start(debug=debug, icon=str(ICON) if ICON.is_file() else None)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        thread.join(10)
    return 0
