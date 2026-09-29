"""The local HTTP + WebSocket server behind the desktop window and the browser UI.

Bound to 127.0.0.1 by default. Every API call and the WebSocket need the
random token printed at start-up (the page receives it in the URL fragment,
which browsers never send to servers). This matters because the server can
run shell commands through the agent.
"""

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from localagent import __version__, skillsync
from localagent.attachments import MAX_ATTACHMENT_BYTES, AttachmentStore, unplace
from localagent.config import save_secret, scratch_dir
from localagent.models.auto import loaded_model
from localagent.models.discovery import loaded_models
from localagent.models.registry import Registry, RegistryError
from localagent.server.files_api import make_files_router
from localagent.server.hub import Hub
from localagent.models.sync import sync_local_models
from localagent.remote import Remotes
from localagent.server.models_api import make_router
from localagent.server.remote_api import make_remote_router
from localagent.skills import load_skills, project_skills_dir, user_skills_dir
from localagent.server.runner import Busy, RunRequest, TaskRunner, status_payload
from localagent.store import ChatRecord, Store
from localagent.tools.base import ToolError
from localagent.tools.search import list_files
from localagent.tools.web import WebSettings, describe_settings
from localagent.tools.web import search as web_search
from localagent.worktree import ChatWorktree, git
from localagent.worktree import manager as wm
from localagent.worktree.commit_message import fallback_subject, merge_message_for

STATIC_DIR = Path(__file__).parent / "static"
MAX_DIFF_CHARS = 400_000
MAX_DIR_ENTRIES = 500
MAX_SETTINGS = 50
NOT_BUILT = """<!doctype html><title>LocalAgent</title><body style="font-family:system-ui;padding:2em">
<h2>The LocalAgent UI has not been built</h2><p>Run <code>npm install && npm run build</code> in
<code>frontend/</code>, then restart <code>la ui</code>.</p></body>"""


def _loaded_ids(registry: Registry) -> set[str]:
    """Model ids a local server currently holds in memory (LM Studio reports it).

    It matters on a single-GPU server: choosing a model that is not loaded makes
    the server load it, which evicts the one that is loaded.
    """
    loaded: set[str] = set()
    for provider in registry.providers.values():
        if not provider.is_local():
            continue
        try:
            loaded |= {m.id for m in loaded_models(provider, timeout=2.0)}
        except (ConnectionError, OSError):
            pass  # the server is off; the UI just shows no "loaded" mark
    return loaded


def _auto_model(registry: Registry) -> str | None:
    """What the ``auto`` role target resolves to right now, if it can be resolved."""
    try:
        return loaded_model(registry).name
    except (RegistryError, ConnectionError, OSError):
        return None


def _registry_payload() -> dict[str, Any]:
    try:
        registry = Registry.load()
    except RegistryError as e:
        return {"error": str(e), "providers": [], "models": [], "roles": {}}
    sync_local_models(registry)  # models come and go in LM Studio; the list follows the server
    loaded = _loaded_ids(registry)
    return {
        "providers": [{"name": p.name, "base_url": p.base_url, "max_concurrency": p.max_concurrency,
                       "local": p.is_local()} for p in registry.providers.values()],
        "models": [{"name": m.name, "provider": m.provider, "context_length": m.context_length,
                    "thinking": m.thinking, "notes": m.notes, "vision": m.vision, "switchable": m.switchable,
                    "local": registry.providers[m.provider].is_local(), "loaded": m.model_id in loaded}
                   for m in registry.models.values()],
        "roles": {name: asdict(role) for name, role in registry.roles.items()},
        "auto_model": _auto_model(registry),
    }


def _dir_listing(path: str, store: Store) -> dict[str, Any]:
    if not path:
        places = [str(Path.home())]
        if hasattr(os, "listdrives"):
            places += os.listdrives()
        return {"path": "", "parent": None, "entries": [{"name": p, "path": p, "repo": False} for p in places],
                "recent": _recent_folders(store)}
    directory = Path(path).expanduser().resolve()
    if not directory.is_dir():
        raise HTTPException(404, f"{directory} is not a directory")
    entries = []
    try:
        children = sorted((c for c in directory.iterdir() if c.is_dir() and not c.name.startswith(".")),
                          key=lambda c: c.name.lower())
    except PermissionError:
        children = []
    for child in children[:MAX_DIR_ENTRIES]:
        entries.append({"name": child.name, "path": str(child), "repo": (child / ".git").exists()})
    parent = str(directory.parent) if directory.parent != directory else ""
    return {"path": str(directory), "parent": parent, "entries": entries, "repo": (directory / ".git").exists()}


def _recent_folders(store: Store) -> list[str]:
    """Recently used folders that still exist; the scratch folder has its own button."""
    scratch = str(scratch_dir().resolve())
    return [p for p in store.recent_folders() if p != scratch and Path(p).is_dir()]


def _own_placements(store: Store, chat: ChatRecord) -> set[str]:
    """The attachment copies *chat* put in its folder that no other chat of the folder uses.

    Read before the chat's events are deleted. They matter where the chat worked in the folder itself (not a
    git repository, or the scratch folder); a chat on a branch had them in its worktree, which goes with it.
    """
    mine = store.placed_attachments(chat.id)
    for other in store.list_chats(chat.work_dir, limit=-1):
        if mine and other.id != chat.id:
            mine -= store.placed_attachments(other.id)
    return mine


def _chat_worktree(store: Store, chat_id: str) -> ChatWorktree | None:
    chat = store.chat(chat_id)
    if chat is None:
        raise HTTPException(404, f"no chat {chat_id}")
    repo = git.repo_root(Path(chat.work_dir))
    return wm.load(repo, chat_id) if repo is not None else None


def create_app(token: str | None = None, store: Store | None = None, static_dir: Path | None = STATIC_DIR,
               attachments: AttachmentStore | None = None, remotes: Remotes | None = None,
               desktop: bool = False) -> FastAPI:
    """Build the app. ``app.state.token`` holds the access token.

    Args:
        desktop: This server belongs to the desktop window on the same computer, so the page may use
            the window's native dialogs. Another machine's UI can be shown in that window too (the
            machine switcher), and its folders are not on this computer: its server says ``False``.
    """
    token = token or secrets.token_urlsafe(24)
    hub = Hub()
    store = store or Store()
    attachments = attachments or AttachmentStore()
    runner = TaskRunner(store, hub.publish, attachments=attachments)
    # ssh status and prompts go to the page but never into the backlog of the running task.
    remotes = remotes or Remotes(lambda event: hub.publish(event, remember=False))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        hub.bind(asyncio.get_running_loop())
        yield
        runner.stop()
        await asyncio.to_thread(remotes.close_all)  # no ssh process outlives the app

    app = FastAPI(title="LocalAgent", version=__version__, lifespan=lifespan)
    app.state.token, app.state.hub, app.state.runner, app.state.store = token, hub, runner, store
    app.state.remotes = remotes

    @app.exception_handler(git.GitError)
    def git_failed(_: Request, error: git.GitError) -> JSONResponse:
        """Show what git refused instead of an opaque 500 (branch actions are all git)."""
        return JSONResponse({"detail": str(error)}, status_code=409)

    def authorize(request: Request) -> None:
        header = request.headers.get("authorization", "")
        supplied = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else request.query_params.get("token", "")
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(401, "missing or wrong token")

    api = Depends(authorize)
    app.include_router(make_router(api))
    app.include_router(make_remote_router(api, remotes))
    app.include_router(make_files_router(api, store))

    @app.post("/api/attachments", dependencies=[api])
    async def upload(request: Request, name: str = Query(..., max_length=255)) -> dict[str, Any]:
        """Store an uploaded file (the raw request body) and return its metadata."""
        if int(request.headers.get("content-length") or 0) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(413, f"attachments are limited to {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB")
        data = await request.body()
        mime = request.headers.get("content-type", "").split(";")[0].strip()
        try:
            stored = await asyncio.to_thread(attachments.add, data, name, "" if mime == "application/octet-stream" else mime)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return stored.to_dict()

    @app.get("/api/attachments/{attachment_id}", dependencies=[api])
    def attachment(attachment_id: str) -> FileResponse:
        try:
            stored = attachments.get(attachment_id)
        except KeyError:
            raise HTTPException(404, "no such attachment") from None
        return FileResponse(attachments.content_path(stored), media_type=stored.mime, filename=stored.name,
                            content_disposition_type="inline")

    @app.get("/api/scratch", dependencies=[api])
    def scratch() -> dict[str, str]:
        """The folder for chats with no project (created on demand)."""
        return {"path": str(scratch_dir())}

    @app.get("/api/files", dependencies=[api])
    def files(work_dir: str = Query(...), limit: int = 4000) -> dict[str, Any]:
        """Files in the folder for @-mentions (same listing the agent's tools see)."""
        root = Path(work_dir).expanduser().resolve()
        if not root.is_dir():
            raise HTTPException(404, f"{root} is not a directory")
        found = [p.relative_to(root).as_posix() for p in list_files(root)[:limit]]
        return {"files": found, "truncated": len(found) >= limit}

    @app.get("/api/state", dependencies=[api])
    def state() -> dict[str, Any]:
        return {**runner.state(), "version": __version__, "native_dialogs": desktop}

    @app.get("/api/models", dependencies=[api])
    def models() -> dict[str, Any]:
        return _registry_payload()

    @app.get("/api/repo", dependencies=[api])
    def repo(work_dir: str = Query(...)) -> dict[str, Any]:
        payload = status_payload(work_dir)
        payload["branches"] = git.local_branches(Path(payload["repo"])) if payload["repo"] else []
        return payload

    @app.get("/api/skills", dependencies=[api])
    def skills(work_dir: str = Query(...)) -> dict[str, Any]:
        """What the model can open for this folder. Read-only: skills are files the user manages."""
        found = load_skills(Path(work_dir).expanduser())
        return {
            "skills": [{"name": s.name, "description": s.description, "source": s.source,
                        "folder": str(s.folder)} for s in found],
            "user_dir": str(user_skills_dir()),
            "project_dir": str(project_skills_dir(Path(work_dir).expanduser())),
        }

    @app.put("/api/skills/synced", dependencies=[api])
    async def receive_skills(request: Request, origin: str = Query(..., max_length=64)) -> dict[str, Any]:
        """The user skills of the computer whose app is logged in to this machine (see ``skillsync``)."""
        if int(request.headers.get("content-length") or 0) > skillsync.MAX_BYTES:
            raise HTTPException(413, "too large")
        data = await request.body()
        try:
            count = await asyncio.to_thread(skillsync.unpack, data, origin)
        except skillsync.SyncError as e:
            raise HTTPException(400, str(e)) from None
        return {"skills": count}

    @app.get("/api/web", dependencies=[api])
    def web_settings() -> dict[str, Any]:
        return describe_settings(WebSettings.load())

    @app.put("/api/web", dependencies=[api])
    def put_web_settings(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Change the web tools' settings; ``api_key`` goes to secrets.env and is never sent back."""
        settings = WebSettings(enabled=bool(body.get("enabled", True)), search=str(body.get("search") or ""),
                               url=str(body.get("url") or "").strip(),
                               api_key_env=str(body.get("api_key_env") or "").strip())
        try:
            settings.save()
            if str(body.get("api_key") or "").strip():
                if not settings.key_env:
                    raise ValueError(f"{settings.search} takes no API key")
                save_secret(settings.key_env, str(body["api_key"]))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return describe_settings(settings)

    @app.post("/api/web/search", dependencies=[api])
    def try_web_search(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Search once with the saved settings, so the settings page can show that they work."""
        try:
            results = web_search(WebSettings.load(), str(body.get("query") or "LocalAgent"), 5)
        except ToolError as e:
            raise HTTPException(502, str(e)) from None
        return {"results": [asdict(r) for r in results]}

    @app.get("/api/settings", dependencies=[api])
    def settings() -> dict[str, Any]:
        """What the page remembers between starts (see ``Store.settings``)."""
        return store.settings()

    @app.put("/api/settings", dependencies=[api])
    def put_settings(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Store the given keys (null removes one) and return everything."""
        if len(body) > MAX_SETTINGS or any(len(k) > 200 for k in body):
            raise HTTPException(400, "too many settings at once")
        store.save_settings(body)
        return store.settings()

    @app.get("/api/recent", dependencies=[api])
    def recent() -> dict[str, Any]:
        """Recent folders, newest first: the window opens the first one when it is not told a folder."""
        return {"folders": _recent_folders(store), "scratch": str(scratch_dir())}

    @app.post("/api/recent", dependencies=[api])
    def open_folder(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Put a folder the UI opened first on the recent list."""
        path = Path(str(body.get("path") or "")).expanduser()
        if not str(body.get("path") or "").strip() or not path.is_dir():
            raise HTTPException(404, f"{path} is not a directory")
        store.open_folder(path)
        return recent()

    @app.delete("/api/recent", dependencies=[api])
    def forget_folder(path: str = Query(...)) -> dict[str, Any]:
        store.forget_folder(path)
        return recent()

    @app.get("/api/fs", dependencies=[api])
    def filesystem(path: str = "") -> dict[str, Any]:
        return _dir_listing(path, store)

    @app.get("/api/chats", dependencies=[api])
    def chats(work_dir: str = Query(...), limit: int = 50) -> list[dict[str, Any]]:
        return [asdict(c) for c in store.list_chats(work_dir, limit=limit)]

    @app.get("/api/chats/{chat_id}", dependencies=[api])
    def chat(chat_id: str) -> dict[str, Any]:
        record = store.chat(chat_id)
        if record is None:
            raise HTTPException(404, f"no chat {chat_id}")
        tasks = [{**asdict(t), "events": store.events(t.id)} for t in store.tasks(chat_id)]
        return {"chat": asdict(record), "tasks": tasks}

    @app.delete("/api/chats/{chat_id}", dependencies=[api])
    def delete_chat(chat_id: str, discard_branch: bool = False) -> dict[str, Any]:
        """Delete a chat and its transcript.

        A chat whose branch still holds work is refused until the user says to throw that away too:
        the transcript is easy to lose, the commits are not.
        """
        record = store.chat(chat_id)
        if record is None:
            raise HTTPException(404, f"no chat {chat_id}")
        if runner.running_chat() == chat_id:
            raise HTTPException(409, "the chat is running; stop the task first")
        wt = _chat_worktree(store, chat_id)
        notes: list[str] = []
        if wt is not None:
            # A branch that holds nothing goes quietly; one with work on it needs a second yes.
            commits = len(wm.commits(wt))
            unsaved = (wt.path / ".git").exists() and git.is_dirty(wt.path)
            if not discard_branch and (commits or unsaved):
                held = f"{commits} commit(s)" if commits else "uncommitted changes"
                raise HTTPException(409, f"{wt.branch} still has {held} on top of {wt.base}. "
                                         "Merge or keep them first, or delete the chat with its branch.")
            notes = wm.discard(wt)
            hub.publish({"type": "repo", "work_dir": str(wt.repo), **status_payload(wt.repo)}, remember=False)
        placed = _own_placements(store, record)
        tasks = store.delete_chat(chat_id)
        unplace(Path(record.work_dir), placed)
        return {"tasks": tasks, "branch": wt.branch if wt else None, "notes": notes}

    @app.get("/api/orphaned-chats", dependencies=[api])
    def orphaned_chats() -> list[dict[str, Any]]:
        """Chats whose folder no longer exists, for the page's clean-up dialog."""
        return [asdict(c) for c in store.orphaned_chats()]

    @app.post("/api/orphaned-chats/delete", dependencies=[api])
    def delete_orphaned_chats(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Delete the chosen chats whose folder is gone, with what only they used.

        The folder is checked again here, so a chat whose folder exists is never deleted this way, nor a
        running one. Only records go: the tasks and events, the folder's row in the recent list and its model
        setting (once no chat of that folder is left), and attachments no remaining task refers to. A chat
        branch in a repository that still exists is left alone; the repository's status keeps listing it.
        """
        wanted = {str(i) for i in body.get("ids") or []}
        chosen = [c for c in store.orphaned_chats() if c.id in wanted and runner.running_chat() != c.id]
        attached = {a["id"] for c in chosen for t in store.tasks(c.id, parent_id=None) for a in t.attachments
                    if a.get("id")}
        tasks = sum(store.delete_chat(c.id) for c in chosen)
        remaining = {c.work_dir for c in store.list_chats(limit=-1)}
        folders = {c.work_dir for c in chosen} - remaining
        for folder in folders:
            store.delete_folder_record(folder)
        store.save_settings({f"model:{folder}": None for folder in folders})
        unused = attached - store.attachments_in_use()
        for attachment_id in unused:
            attachments.remove(attachment_id)
        return {"chats": len(chosen), "tasks": tasks, "attachments": len(unused)}

    @app.get("/api/chats/{chat_id}/changes", dependencies=[api])
    def changes(chat_id: str) -> dict[str, Any]:
        wt = _chat_worktree(store, chat_id)
        if wt is None:
            return {"branch": None}
        diff = wm.diff(wt)
        return {
            "branch": wt.branch, "base": wt.base, "checked_out": (wt.path / ".git").exists(),
            "commits": [list(c) for c in wm.commits(wt)], "stat": wm.diff(wt, stat=True),
            "diff": diff[:MAX_DIFF_CHARS], "truncated": len(diff) > MAX_DIFF_CHARS,
        }

    def _branch_action(chat_id: str) -> ChatWorktree:
        if runner.running_chat() == chat_id:
            raise HTTPException(409, "the chat is running; wait for the task to finish")
        wt = _chat_worktree(store, chat_id)
        if wt is None:
            raise HTTPException(404, "the chat has no pending branch")
        return wt

    @app.post("/api/chats/{chat_id}/merge", dependencies=[api])
    def merge(chat_id: str, plain: bool = False) -> dict[str, Any]:
        wt = _branch_action(chat_id)
        try:
            registry = None if plain else Registry.load()
        except RegistryError:
            registry = None
        tasks = [t.task for t in store.tasks(chat_id)]
        result = wm.merge(wt, merge_message_for(registry, tasks, wt.branch))
        hub.publish({"type": "repo", "work_dir": str(wt.repo), **status_payload(wt.repo)}, remember=False)
        return asdict(result)

    @app.post("/api/chats/{chat_id}/keep", dependencies=[api])
    def keep(chat_id: str) -> dict[str, Any]:
        wt = _branch_action(chat_id)
        tasks = [t.task for t in store.tasks(chat_id)]
        committed, notes = wm.keep(wt, lambda diff: fallback_subject(tasks[-1] if tasks else "work in progress"))
        return {"committed": list(committed) if committed else None, "notes": notes}

    @app.post("/api/chats/{chat_id}/discard", dependencies=[api])
    def discard(chat_id: str) -> dict[str, Any]:
        wt = _branch_action(chat_id)
        notes = wm.discard(wt)
        hub.publish({"type": "repo", "work_dir": str(wt.repo), **status_payload(wt.repo)}, remember=False)
        return {"notes": notes}

    request_fields = {f.name for f in fields(RunRequest)}

    async def handle(message: dict[str, Any], websocket: WebSocket) -> None:
        kind = message.get("type")
        if kind == "run":
            try:
                request = RunRequest(**{k: v for k, v in message.items() if k in request_fields})
                chat_id = await asyncio.to_thread(runner.start, request, hub.reset_backlog)
            except (Busy, ValueError, TypeError, RegistryError) as e:
                await websocket.send_json({"type": "error", "message": str(e)})
                return
            await websocket.send_json({"type": "accepted", "chat_id": chat_id})
        elif kind == "steer":
            try:
                runner.steer(str(message.get("text", "")), [str(i) for i in message.get("attachments") or []])
            except ValueError as e:
                await websocket.send_json({"type": "error", "message": str(e)})
        elif kind == "set_model":
            runner.set_model(str(message.get("model", "")))
        elif kind == "stop":
            runner.stop()
        elif kind == "approval":
            runner.approvals.answer(str(message.get("id", "")), bool(message.get("allow")))
        elif kind == "answer":
            runner.questions.answer(str(message.get("id", "")), str(message.get("text", "")))
        else:
            await websocket.send_json({"type": "error", "message": f"unknown command {kind!r}"})

    @app.websocket("/ws")
    async def events(websocket: WebSocket) -> None:
        if not secrets.compare_digest(websocket.query_params.get("token", ""), token):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        queue, backlog = hub.subscribe()
        await websocket.send_json({"type": "hello", "state": {**runner.state(), "native_dialogs": desktop},
                                   "backlog": backlog})

        async def send() -> None:
            while True:
                await websocket.send_json(await queue.get())

        sender = asyncio.create_task(send())
        try:
            while True:
                await handle(await websocket.receive_json(), websocket)
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            hub.unsubscribe(queue)

    if static_dir is not None and (static_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")
    else:
        @app.get("/")
        def not_built() -> HTMLResponse:
            return HTMLResponse(NOT_BUILT)

    return app
