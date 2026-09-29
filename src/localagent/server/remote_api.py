"""Routes for other machines: manage them, connect, and relay ssh's questions.

Everything here needs the UI token except ``/api/askpass``, which ``la-askpass``
calls from inside the ssh process we started. That caller has no UI token and
must not get one; it proves itself with the one-time secret of its ssh process
instead (see ``remote.Remotes.ask``), and only from this computer.
"""

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

from localagent.remote import Remotes

LOOPBACK = frozenset({"127.0.0.1", "::1"})


def make_remote_router(auth: Any, remotes: Remotes) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/remotes", dependencies=[auth])
    def list_remotes() -> dict[str, Any]:
        return remotes.view()

    @router.put("/remotes/{name}", dependencies=[auth])
    def put_remote(name: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            remotes.save(name, str(body.get("host") or ""), list(body.get("forwards") or []),
                         str(body.get("ui") or ""), dict(body.get("commands") or {}))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return remotes.view()

    @router.delete("/remotes/{name}", dependencies=[auth])
    def delete_remote(name: str) -> dict[str, Any]:
        remotes.remove(name)
        return remotes.view()

    @router.post("/remotes/{name}/connect", dependencies=[auth])
    def connect(name: str, request: Request) -> dict[str, Any]:
        # la-askpass reaches this same server; always over loopback, whatever address the page used.
        port = (request.scope.get("server") or (None, request.url.port))[1]
        remotes.askpass_url = f"http://127.0.0.1:{port}/api/askpass"
        try:
            remotes.connect(name)
        except KeyError:
            raise HTTPException(404, f"no machine named {name!r}") from None
        return remotes.view()

    @router.post("/remotes/{name}/run", dependencies=[auth])
    def run_command(name: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Run one of the machine's named commands (remotes.yaml) and return what it printed.

        The body names a command; it never carries a command line, so this endpoint can only
        set off what the user has written in remotes.yaml for that machine.
        """
        port = (request.scope.get("server") or (None, request.url.port))[1]
        remotes.askpass_url = f"http://127.0.0.1:{port}/api/askpass"  # its ssh may have to ask for a password
        try:
            return remotes.run(name, str(body.get("command") or ""))
        except KeyError as e:
            raise HTTPException(404, f"no machine or command named {e.args[0]!r}") from None

    @router.post("/remotes/{name}/disconnect", dependencies=[auth])
    def disconnect(name: str) -> dict[str, Any]:
        remotes.disconnect(name)
        return remotes.view()

    @router.post("/ssh-prompts/{prompt_id}", dependencies=[auth])
    def answer_prompt(prompt_id: str, body: dict[str, Any] = Body(...)) -> dict[str, bool]:
        answer = None if body.get("cancel") else str(body.get("answer", ""))
        if not remotes.respond(prompt_id, answer):
            raise HTTPException(404, "that question is no longer waiting")
        return {"ok": True}

    @router.post("/askpass")
    def askpass(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, str | None]:
        if request.client is None or request.client.host not in LOOPBACK:
            raise HTTPException(403, "only from this computer")
        try:
            answer = remotes.ask(request.headers.get("x-la-askpass", ""), str(body.get("prompt", "")))
        except PermissionError:
            raise HTTPException(403, "not an ssh process this app started") from None
        return {"answer": answer}

    return router
