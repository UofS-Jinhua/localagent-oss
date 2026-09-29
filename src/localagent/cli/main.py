"""The ``la`` command line."""

import argparse
import getpass
import shutil
import sys
import threading
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Confirm
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from localagent.agent import ChatSession
from localagent.attachments import AttachmentStore
from localagent.cli.console_input import ConsoleInput
from localagent.cli.output import prepare_streams
from localagent.cli.render import TerminalRenderer
from localagent.config import get_secret, read_secrets, registry_path, save_secret, scratch_dir
from localagent.fsutil import remove_tree
from localagent.models import make_client
from localagent.models.discovery import discover, is_lmstudio
from localagent.models.presets import PRESETS, chat_models, normalize_base_url, spec_from_discovery
from localagent.models.probe import run_probes, run_quick_probes
from localagent.models.registry import AUTO_MODEL, Provider, Registry, RegistryError, RoleSpec, is_auto
from localagent.models.sync import loaded_now, sync_local_models
from localagent.remote import Remotes, remotes_path
from localagent.skills import (
    SKILL_FILE,
    load_skills,
    project_skills_dir,
    read_skill,
    skill_text,
    user_skills_dir,
)
from localagent.store import Store
from localagent.tools.base import ToolError
from localagent.tools.web import BACKENDS, WebSettings, format_results, make_web_tools, web_path
from localagent.tools.web import search as web_search
from localagent.worktree import ChatWorktree, git
from localagent.worktree import manager as wm
from localagent.worktree.commit_message import fallback_subject, merge_message_for

console = Console()
PRESET_BY_ID = {p.id: p for p in PRESETS}


def _load_or_empty() -> Registry:
    try:
        return Registry.load()
    except RegistryError:
        if registry_path().is_file():
            raise
        return Registry()


def cmd_init(args: argparse.Namespace) -> int:
    registry = _load_or_empty()
    preset = PRESET_BY_ID.get(args.preset) if args.preset else None
    if args.preset and preset is None:
        console.print(f"[red]unknown preset {args.preset!r}; choose from {', '.join(PRESET_BY_ID)}[/]")
        return 1
    if not args.base_url and preset is None:
        console.print("[red]give --base-url or --preset[/]")
        return 1
    base_url = normalize_base_url(args.base_url or preset.base_url)
    discovery = preset.discovery if preset else ""
    if not discovery and is_lmstudio(base_url):
        discovery = "lmstudio"
    name = args.provider or (preset.id if preset else "lmstudio" if discovery == "lmstudio" else "local")
    if name in registry.providers and not args.force:
        console.print(f"[red]provider {name!r} already exists; use --force to replace it or --provider to pick another name[/]")
        return 1
    provider = Provider(
        name=name, base_url=base_url, api_key_env=args.api_key_env or (preset.api_key_env if preset else ""),
        max_concurrency=args.max_concurrency or (preset.max_concurrency if preset else 2), discovery=discovery,
    )
    if provider.api_key_env and not provider.resolve_api_key():
        console.print(f"[red]no key in ${provider.api_key_env}; store one with `la secret set {provider.api_key_env}`[/]")
        return 1
    try:
        found = chat_models(discover(provider))
    except ConnectionError as e:
        console.print(f"[red]{e}[/]")
        return 1
    if args.only:
        wanted = {m.strip() for m in args.only.split(",") if m.strip()}
        found = [m for m in found if m.id in wanted]
        missing = wanted - {m.id for m in found}
        if missing:
            console.print(f"[yellow]not offered by the endpoint: {', '.join(sorted(missing))}[/]")
    elif not provider.is_local() and len(found) > 5:
        console.print(f"[red]the endpoint offers {len(found)} models; pick some with --only id1,id2[/]")
        console.print(", ".join(m.id for m in found))
        return 1
    if not found:
        console.print("[red]the endpoint reports no chat models[/]")
        return 1
    registry.providers[name] = provider
    for m in found:
        registry.models.setdefault(m.id, spec_from_discovery(m, provider))
    # A server that reports what it has loaded gets "auto": the roles follow whatever
    # is loaded there, instead of pinning a name and making it swap models later.
    default = AUTO_MODEL if provider.discovery == "lmstudio" else next((m.id for m in found if m.loaded), found[0].id)
    registry.roles.setdefault("main", RoleSpec(model=default, thinking=True))
    registry.roles.setdefault("subagent", RoleSpec(model=default, thinking=True))
    registry.roles.setdefault("summarizer", RoleSpec(model=default, thinking=False))
    registry.roles.setdefault("commit_message", RoleSpec(model=default, thinking=False))
    path = registry.save()
    console.print(f"[green]saved {len(found)} model(s) from {base_url} to {path}[/]")
    _print_registry(registry)
    return 0


def _print_registry(registry: Registry) -> None:
    table = Table(title="models")
    for column in ("name", "provider", "context", "loaded", "thinking", "vision", "set_model", "notes"):
        table.add_column(column)
    live = loaded_now(registry)
    for m in registry.models.values():
        here = live.get((m.provider, m.model_id))
        # A loaded model's window is whatever LM Studio gave it, which is what a run would get.
        context = (here.context_length if here and here.context_length else m.context_length)
        table.add_row(m.name, m.provider, f"{context:,}", "yes" if here else "", m.thinking,
                      "yes" if m.vision else "", "yes" if m.switchable else "", m.notes)
    console.print(table)
    roles = Table(title="roles")
    roles.add_column("role")
    roles.add_column("model")
    roles.add_column("thinking")
    resolved = ""
    if any(is_auto(r.model) for r in registry.roles.values()):
        try:
            resolved = registry.resolve().model.name
        except RegistryError as e:
            resolved = f"[red]{e}[/]"
    for name, r in registry.roles.items():
        model = f"{r.model} → {resolved}" if is_auto(r.model) and resolved else r.model
        roles.add_row(name, model, {True: "on", False: "off", None: "default"}[r.thinking])
    console.print(roles)


def _sync(registry: Registry, force: bool = False) -> None:
    """Follow the local servers' model lists, and say so when something changed."""
    result = sync_local_models(registry, force=force)
    if result.changed() or result.kept:
        console.print(f"[dim]models: {result.describe()}[/]")


def cmd_models_list(args: argparse.Namespace) -> int:
    registry = Registry.load()
    _sync(registry, force=True)  # what a local server holds is its business, not a stored setting
    providers = Table(title=f"providers ({registry.path})")
    for column in ("name", "base_url", "where", "key", "max_concurrency", "discovery"):
        providers.add_column(column)
    for p in registry.providers.values():
        key = "-" if not p.api_key_env and not p.api_key else "set" if p.resolve_api_key() else f"missing ${p.api_key_env}"
        providers.add_row(p.name, p.base_url, "local" if p.is_local() else "cloud", key, str(p.max_concurrency),
                          p.discovery or "-")
    console.print(providers)
    _print_registry(registry)
    return 0


def cmd_models_discover(args: argparse.Namespace) -> int:
    registry = Registry.load()
    provider = registry.providers.get(args.provider)
    if provider is None:
        console.print(f"[red]unknown provider {args.provider!r}[/]")
        return 1
    found = discover(provider)
    table = Table(title=f"models at {provider.base_url}")
    for column in ("id", "type", "loaded", "context", "tool use", "quantization", "registered"):
        table.add_column(column)
    for m in found:
        table.add_row(
            m.id, m.type, {True: "yes", False: "no", None: "?"}[m.loaded],
            f"{m.context_length:,}" if m.context_length else "-",
            {True: "yes", False: "no", None: "?"}[m.tool_use], m.quantization,
            "yes" if m.id in registry.models else "",
        )
    console.print(table)
    if args.add:
        candidates = chat_models(found)
        if args.only:
            wanted = {m.strip() for m in args.only.split(",")}
            candidates = [m for m in candidates if m.id in wanted]
        added = [m for m in candidates if m.id not in registry.models]
        for m in added:
            registry.models[m.id] = spec_from_discovery(m, provider)
        # Refresh what the endpoint knows better than the registry: whether the model can see,
        # and the context length it is actually loaded with (LM Studio reports it only while loaded).
        updated = []
        for found in candidates:
            spec = registry.models.get(found.id)
            if spec is None or spec.provider != provider.name or found.id in {m.id for m in added}:
                continue
            changes = []
            if found.vision and not spec.vision:
                spec.vision, _ = True, changes.append("vision")
            if found.context_length and found.context_length != spec.context_length:
                changes.append(f"context {spec.context_length:,}->{found.context_length:,}")
                spec.context_length = found.context_length
            if changes:
                updated.append(f"{found.id} ({', '.join(changes)})")
        registry.save()
        console.print(f"[green]added {len(added)} model(s)[/]")
        for line in updated:
            console.print(f"[green]updated {line}[/]")
    return 0


def cmd_models_test(args: argparse.Namespace) -> int:
    registry = Registry.load()
    resolved = registry.resolve(model=args.model) if args.model else registry.resolve("main")
    console.print(f"testing [bold]{resolved.model.name}[/] at {resolved.provider.base_url} …")
    client = make_client(resolved)
    try:
        results = run_quick_probes(client) if args.quick else run_probes(client, include_cache=not args.no_cache)
    finally:
        client.close()
    table = Table(title=resolved.model.name)
    table.add_column("probe")
    table.add_column("result")
    table.add_column("detail")
    for r in results:
        table.add_row(r.name, "[green]ok[/]" if r.ok else "[red]fail[/]", r.detail)
    console.print(table)
    return 0 if all(r.ok for r in results) else 1


def _resolve_chat(store: Store, args: argparse.Namespace) -> tuple[str | None, str]:
    """Return ``(chat_id or None, error)`` from --chat / --continue."""
    if args.chat:
        chat_id = args.chat if store.chat_exists(args.chat) else store.find_chat(args.chat)
        return (chat_id, "") if chat_id else (None, f"no chat matches {args.chat!r}; see `la chats`")
    if args.continue_chat:
        chat_id = store.latest_chat(args.work_dir)
        return (chat_id, "") if chat_id else (None, f"no earlier chat in {Path(args.work_dir).resolve()}")
    return None, ""


def cmd_secret(args: argparse.Namespace) -> int:
    if args.secret_command == "list":
        names = sorted(read_secrets())
        console.print("\n".join(names) if names else "no keys stored")
        return 0
    if args.secret_command == "remove":
        save_secret(args.name, "")
        console.print(f"removed {args.name}")
        return 0
    value = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass(f"{args.name}: ")
    if not value:
        console.print("[red]no key given[/]")
        return 1
    path = save_secret(args.name, value)
    console.print(f"stored {args.name} in {path}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    registry = Registry.load()
    _sync(registry)
    if args.scratch:
        args.work_dir = str(scratch_dir())
    store = Store()
    chat_id, error = _resolve_chat(store, args)
    if error:
        console.print(f"[red]{error}[/]")
        return 1
    attachment_store = AttachmentStore()
    try:
        attachments = [attachment_store.add_file(path) for path in args.attach]
    except (OSError, ValueError) as e:
        console.print(f"[red]cannot attach: {e}[/]")
        return 1
    stop = threading.Event()
    console_input = ConsoleInput(console, lambda message: session.steer(message)) if sys.stdin.isatty() else None
    if args.yes:
        approver = lambda command, reason: True  # noqa: E731
    else:
        approver = console_input.confirm if console_input is not None else None
    session = ChatSession(
        registry, args.work_dir, store=store, chat_id=chat_id,
        on_event=TerminalRenderer(console, show_thinking=args.show_thinking),
        isolate=not args.no_worktree, base=args.base, model_commit_messages=not args.plain_commits,
        attachment_store=attachment_store, review=args.review, subtasks=not args.no_subtasks,
        role=args.role, model=args.model,
        thinking=False if args.no_thinking else None, max_steps=args.max_steps, max_sessions=args.max_sessions,
        approver=approver, stop_event=stop, allow_cloud=args.allow_cloud,
        asker=console_input.ask if console_input is not None else None,
    )
    if console_input is not None:
        console_input.start()
        console.print("[dim]Type a message and press Enter to steer the running task; Ctrl+C stops it.[/]")
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.update(result=session.run(" ".join(args.task), attachments)),
                              daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.2)
    except KeyboardInterrupt:
        console.print("[yellow]stopping… press Ctrl+C again to quit immediately[/]")
        stop.set()
        try:
            while worker.is_alive():
                worker.join(0.2)
        except KeyboardInterrupt:
            return 130
    wt = session.worktree
    if wt is not None:
        console.print(f"[cyan]⎇ the changes are on branch [bold]{wt.branch}[/bold] (from {wt.base}); "
                      "your own checkout is untouched.[/]")
        console.print("[dim]  review: la diff · merge: la merge · keep for later: la keep · throw away: la discard[/]")
    console.print(f"[dim]chat {session.chat_id} · continue it with: la run -c -w {args.work_dir} \"...\"[/]")
    result = outcome.get("result")
    return 0 if result is not None and result.success else 1


def _chat_branch(args: argparse.Namespace) -> tuple[ChatWorktree | None, str]:
    """The pending chat branch a command acts on.

    Named by chat id (or prefix) or branch; otherwise the latest chat of the
    working directory if it has a branch, otherwise the only pending one.
    """
    repo = git.repo_root(Path(args.work_dir).resolve())
    if repo is None:
        return None, "not a git repository"
    pending = wm.pending(repo)
    if not pending:
        return None, "no pending chat branches in this repository"
    if args.chat:
        matches = [p for p in pending if p.chat_id.startswith(args.chat) or p.branch == args.chat]
        if len(matches) == 1:
            return matches[0], ""
        return None, f"{len(matches)} pending branches match {args.chat!r}; see `la status`"
    latest = Store().latest_chat(args.work_dir)
    for candidate in pending:
        if candidate.chat_id == latest:
            return candidate, ""
    if len(pending) == 1:
        return pending[0], ""
    return None, "several chat branches are pending (" + ", ".join(p.branch for p in pending) + "); name one"


def _registry_or_none() -> Registry | None:
    try:
        return Registry.load()
    except RegistryError:
        return None


def _chat_tasks(chat_id: str) -> list[str]:
    return [t.task for t in Store().tasks(chat_id)]


def cmd_status(args: argparse.Namespace) -> int:
    status = wm.repo_status(args.work_dir)
    if status.repo is None:
        console.print(f"{Path(args.work_dir).resolve()} is not a git repository")
        return 0
    console.print(f"repository [bold]{status.repo}[/]")
    console.print(f"⎇ current branch: [bold cyan]{status.branch or '(detached HEAD)'}[/] · "
                  f"{status.dirty_files} uncommitted change(s)")
    if not status.pending:
        console.print("no pending chat branches")
        return 0
    store = Store()
    table = Table(title="pending chat branches (la merge / la keep / la discard)")
    for column in ("branch", "from", "commits", "files", "worktree"):
        table.add_column(column, no_wrap=True)
    table.add_column("last task", overflow="fold")
    for p in status.pending:
        tasks = store.tasks(p.chat_id)
        table.add_row(p.branch, p.base, str(p.commits), str(p.files), "yes" if p.checked_out else "kept",
                      tasks[-1].task[:60] if tasks else "")
    console.print(table)
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    wt, error = _chat_branch(args)
    if wt is None:
        console.print(f"[red]{error}[/]")
        return 1
    console.rule(f"{wt.branch} (from {wt.base})")
    for sha, subject in wm.commits(wt):
        console.print(f"[yellow]{sha}[/] {subject}")
    text = wm.diff(wt, stat=args.stat)
    console.print(text if args.stat else Syntax(text, "diff", theme="ansi_dark", word_wrap=True) if text else "(no changes)")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    wt, error = _chat_branch(args)
    if wt is None:
        console.print(f"[red]{error}[/]")
        return 1
    registry = None if args.plain else _registry_or_none()
    console.print(f"merging [bold]{wt.branch}[/] into [bold]{wt.base}[/] …")
    result = wm.merge(wt, merge_message_for(registry, _chat_tasks(wt.chat_id), wt.branch))
    color = {"merged": "green", "nothing": "yellow"}.get(result.status, "red")
    console.print(f"[{color}]{result.message}[/]")
    for note in result.notes:
        console.print(f"[yellow]{note}[/]")
    return 0 if result.status in ("merged", "nothing") else 1


def cmd_keep(args: argparse.Namespace) -> int:
    wt, error = _chat_branch(args)
    if wt is None:
        console.print(f"[red]{error}[/]")
        return 1
    tasks = _chat_tasks(wt.chat_id)
    committed, notes = wm.keep(wt, lambda diff: fallback_subject(tasks[-1] if tasks else "work in progress"))
    extra = f" (committed leftovers as {committed[0][:8]})" if committed else ""
    console.print(f"kept branch [bold]{wt.branch}[/]{extra}; its worktree folder was removed. "
                  "Continue the chat to pick it up again, or merge/discard it later.")
    for note in notes:
        console.print(f"[yellow]{note}[/]")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from localagent.server.desktop import run_ui

    if args.host not in ("127.0.0.1", "localhost"):
        console.print(f"[yellow]listening on {args.host}: anyone who can reach it and has the token can run "
                      "commands on this computer[/]")
    return run_ui(args.work_dir, browser=args.browser, host=args.host, port=args.port, debug=args.debug,
                  open_browser=not args.no_open)


def cmd_discard(args: argparse.Namespace) -> int:
    wt, error = _chat_branch(args)
    if wt is None:
        console.print(f"[red]{error}[/]")
        return 1
    if not args.yes:
        if not sys.stdin.isatty():
            console.print("[red]refusing to discard without confirmation; pass -y[/]")
            return 1
        if not Confirm.ask(f"Throw away branch {wt.branch} and all its changes?", default=False):
            return 1
    notes = wm.discard(wt)
    console.print(f"discarded {wt.branch}; {wt.base} was not changed")
    for note in notes:
        console.print(f"[yellow]{note}[/]")
    return 0


def _when(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _one_line(text: str, limit: int = 60) -> str:
    """Collapse *text* to one clipped line, so a table row stays one row high."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def cmd_skills_list(args: argparse.Namespace) -> int:
    root = Path(args.work_dir).resolve()
    skills = load_skills(root)
    if not skills:
        console.print(f"no skills yet. Put a folder with a {SKILL_FILE} in {user_skills_dir()} "
                      f"(every project) or {project_skills_dir(root)} (this one), or use `la skills add <folder>`.")
        return 0
    table = Table(title=f"skills for {root}")
    table.add_column("name", no_wrap=True)
    table.add_column("from", no_wrap=True)
    table.add_column("what it is for")
    for skill in skills:
        table.add_row(skill.name, skill.source, skill.description or "-")
    console.print(table)
    console.print(f"[dim]the model sees these {len(skills)} line(s); it reads one with Skill(name)[/]")
    return 0


def cmd_skills_show(args: argparse.Namespace) -> int:
    skill = next((s for s in load_skills(Path(args.work_dir).resolve()) if s.name == args.name), None)
    if skill is None:
        console.print(f"[red]no skill named {args.name!r}[/]")
        return 1
    console.print(f"[dim]{skill.path}[/]")
    console.print(Markdown(skill_text(skill)))
    return 0


def cmd_skills_add(args: argparse.Namespace) -> int:
    """Copy a skill folder (or a single SKILL.md) into the user's or the project's skills."""
    source = Path(args.path).expanduser().resolve()
    if source.is_file() and source.name == SKILL_FILE:
        source = source.parent
    if not (source / SKILL_FILE).is_file():
        console.print(f"[red]{source} does not contain a {SKILL_FILE}[/]")
        return 1
    skill = read_skill(source / SKILL_FILE, "project" if args.project else "user")
    if skill is None:
        console.print(f"[red]could not read {source / SKILL_FILE}[/]")
        return 1
    target_dir = project_skills_dir(Path(args.work_dir).resolve()) if args.project else user_skills_dir()
    target = target_dir / skill.name
    if target.exists() and not args.force:
        console.print(f"[red]{target} already exists[/] (use --force to replace it)")
        return 1
    if target.exists():
        remove_tree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    console.print(f"added [bold]{skill.name}[/] to {target}")
    return 0


def cmd_skills_remove(args: argparse.Namespace) -> int:
    skill = next((s for s in load_skills(Path(args.work_dir).resolve()) if s.name == args.name), None)
    if skill is None:
        console.print(f"[red]no skill named {args.name!r}[/]")
        return 1
    if skill.source == "synced":
        console.print(f"[red]{skill.name} is a copy from the computer that logs in here ({skill.folder}); "
                      "remove it on that computer and the next sync removes it here[/]")
        return 1
    remove_tree(skill.folder)
    console.print(f"removed {skill.name} ({skill.folder})")
    return 0


def _remotes() -> "Remotes":
    from localagent.remote import Remotes

    return Remotes(lambda event: None)  # no UI listening: ssh asks on this terminal


def cmd_remote_list(args: argparse.Namespace) -> int:
    from localagent.remote import load_remotes

    specs = load_remotes()
    if not specs:
        console.print(f"no machines yet; add one in the app, or write {remotes_path()}")
        return 0
    table = Table(title=f"machines ({remotes_path()})")
    for column in ("name", "ssh host", "ports", "commands"):
        table.add_column(column, no_wrap=column != "commands")
    for spec in specs.values():
        table.add_row(spec.name, spec.host, ", ".join(f.label() for f in spec.forwards),
                      ", ".join(spec.commands) or "-")
    console.print(table)
    console.print("[dim]run one with `la remote run <machine> <command>`[/]")
    return 0


def cmd_remote_run(args: argparse.Namespace) -> int:
    """Run a command the machine already has a name for, over ssh, and show what it printed."""
    from localagent.remote import load_remotes

    specs = load_remotes()
    spec = specs.get(args.machine)
    if spec is None:
        console.print(f"[red]no machine named {args.machine!r}[/] (have: {', '.join(specs) or 'none'})")
        return 1
    if args.command_name not in spec.commands:
        console.print(f"[red]{args.machine} has no command {args.command_name!r}[/] "
                      f"(have: {', '.join(spec.commands) or 'none'})")
        return 1
    result = _remotes().run(args.machine, args.command_name)
    if result["output"]:
        console.print(result["output"], markup=False, highlight=False)
    code = result["exit_code"]
    if code:
        console.print(f"[red]{args.machine}: {args.command_name} exited with {code}[/]")
    return 1 if code else 0


def cmd_web_show(args: argparse.Namespace) -> int:
    settings = WebSettings.load()
    if not settings.enabled:
        console.print(f"web tools: [bold]off[/] ({web_path()})")
        return 0
    line = f"web tools: on · search: [bold]{settings.search}[/]"
    if settings.endpoint():
        line += f" at {settings.endpoint()}"
    if settings.key_env:
        line += f" · key {settings.key_env}: " + ("stored" if get_secret(settings.key_env) else "[red]missing[/]")
    console.print(line)
    return 0


def cmd_web_set(args: argparse.Namespace) -> int:
    settings = WebSettings.load()
    if args.search is not None:
        settings.search = args.search
    if args.url is not None:
        settings.url = args.url.strip()
    if args.key_env is not None:
        settings.api_key_env = args.key_env.strip()
    if args.enabled is not None:
        settings.enabled = args.enabled
    try:
        settings.save()
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        return 1
    return cmd_web_show(args)


def cmd_web_search(args: argparse.Namespace) -> int:
    query = " ".join(args.query)
    try:
        console.print(format_results(query, web_search(WebSettings.load(), query)), markup=False, highlight=False)
    except ToolError as e:
        console.print(f"[red]{e}[/]")
        return 1
    return 0


def cmd_web_fetch(args: argparse.Namespace) -> int:
    tool = next((t for t in make_web_tools(WebSettings(search="off")) if t.name == "WebFetch"))
    try:
        console.print(tool.call({"url": args.url, "start": args.start}), markup=False, highlight=False)
    except ToolError as e:
        console.print(f"[red]{e}[/]")
        return 1
    return 0


def cmd_chats(args: argparse.Namespace) -> int:
    chats = Store().list_chats(None if args.all else args.work_dir, limit=args.limit)
    if not chats:
        console.print("no chats yet" + ("" if args.all else f" in {Path(args.work_dir).resolve()} (use --all)"))
        return 0
    # One row per chat: the task is clipped and never wrapped, so a long task cannot push the table
    # apart. The working directory is a column only with --all; otherwise every row would repeat it.
    table = Table(title="chats" if args.all else f"chats in {Path(args.work_dir).resolve()}")
    table.add_column("id", no_wrap=True, min_width=12)  # never shortened: it is what you type to continue
    for column in ("created", "tasks", "branch"):
        table.add_column(column, no_wrap=True)
    table.add_column("last task", no_wrap=True, overflow="ellipsis", max_width=40)
    table.add_column("last result", no_wrap=True)
    if args.all:
        table.add_column("work dir", no_wrap=True, overflow="ellipsis", max_width=40)
    for c in chats:
        branch = f"{c.branch} ← {c.base}" if c.branch else "-"
        row = [c.id, _when(c.created), str(c.task_count), branch, _one_line(c.last_task, 40), c.last_reason]
        table.add_row(*row, *([c.work_dir] if args.all else []))
    console.print(table)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = Store()
    chat_id = args.chat if store.chat_exists(args.chat) else store.find_chat(args.chat)
    if chat_id is None:
        console.print(f"[red]no chat matches {args.chat!r}[/]")
        return 1
    total_tokens = total_cost = 0
    for number, task in enumerate(store.tasks(chat_id), 1):
        status = {True: "[green]✔[/]", False: "[red]✘[/]", None: "[yellow]…[/]"}[task.success]
        total_tokens += task.total_tokens
        total_cost += task.cost
        spend = f" · {task.total_tokens:,} tokens" + (f" · ${task.cost:.3f}" if task.cost else "")
        console.rule(f"{status} task {number} · {_when(task.created)} · {task.reason} · {task.steps} steps"
                     f"{spend} · {task.elapsed:.0f}s · {task.model}")
        console.print(Text(task.task, style="bold"))
        for child in store.tasks(chat_id, parent_id=task.id):
            mark = "[green]✔[/]" if child.success else "[red]✘[/]"
            console.print(f"  {mark} [dim]sub-agent · {child.steps} steps · {child.total_tokens:,} tokens ·"
                          f" {_one_line(child.task, 70)}[/]")
        if task.result:
            console.print(Markdown(task.result))
    if total_tokens:
        spent = f" · ${total_cost:.2f}" if total_cost else ""
        console.print(f"[dim]chat total: {total_tokens:,} tokens{spent}[/]")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="la", description="LocalAgent: a coding agent for local LLMs.")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="add an endpoint (LM Studio, Ollama, vLLM, Gemini, OpenAI, ...) to the registry")
    init.add_argument("--base-url", help="e.g. http://localhost:1234 or http://host:1234/v1")
    init.add_argument("--preset", help=f"a known service: {', '.join(PRESET_BY_ID)}")
    init.add_argument("--provider", help="provider name (default: the preset, lmstudio or local)")
    init.add_argument("--api-key-env", default="", help="variable holding the API key (environment or `la secret set`)")
    init.add_argument("--only", help="register only these model ids (comma-separated)")
    init.add_argument("--max-concurrency", type=int, default=0, help="parallel requests (default: 2, 1 for cloud presets)")
    init.add_argument("--force", action="store_true", help="replace a provider with the same name")
    init.set_defaults(func=cmd_init)

    secret = sub.add_parser("secret", help="store API keys in ~/.localagent/secrets.env (not in models.yaml)")
    ssub = secret.add_subparsers(dest="secret_command", required=True)
    sset = ssub.add_parser("set", help="store a key (typed hidden, or piped on stdin)")
    sset.add_argument("name", help="variable name, e.g. GEMINI_API_KEY")
    sremove = ssub.add_parser("remove", help="delete a stored key")
    sremove.add_argument("name")
    ssub.add_parser("list", help="names of the stored keys")
    secret.set_defaults(func=cmd_secret)

    models = sub.add_parser("models", help="inspect and test registered models")
    msub = models.add_subparsers(dest="models_command", required=True)
    msub.add_parser("list", help="show providers, models and roles").set_defaults(func=cmd_models_list)
    disc = msub.add_parser("discover", help="list the models a provider serves")
    disc.add_argument("provider")
    disc.add_argument("--add", action="store_true", help="register the chat models that are missing")
    disc.add_argument("--only", help="with --add: only these model ids (comma-separated)")
    disc.set_defaults(func=cmd_models_discover)
    test = msub.add_parser("test", help="probe tool calling, prefill, prompt cache, speed, thinking toggle, vision")
    test.add_argument("model", nargs="?", help="model name (default: the main role's model)")
    test.add_argument("--no-cache", action="store_true", help="skip the slower prefill/cache probe")
    test.add_argument("--quick", action="store_true", help="only tool calling (and vision): 1-2 requests, for cloud quotas")
    test.set_defaults(func=cmd_models_test)

    run = sub.add_parser("run", help="run a task in a working directory")
    run.add_argument("task", nargs="+")
    run.add_argument("-w", "--work-dir", default=".")
    run.add_argument("--scratch", action="store_true",
                     help="work in ~/.localagent/scratch instead of a project (quick questions, one-off scripts)")
    run.add_argument("-c", "--continue", dest="continue_chat", action="store_true",
                     help="continue the latest chat of this working directory")
    run.add_argument("--chat", help="continue the chat with this id (or unique prefix)")
    run.add_argument("-m", "--model", help="model name (default: the role's model)")
    run.add_argument("--role", default="main")
    run.add_argument("--max-steps", type=int, default=100, help="step limit across all sessions")
    run.add_argument("--max-sessions", type=int, default=5, help="how many context windows a task may use")
    run.add_argument("--no-thinking", action="store_true", help="disable thinking for this run")
    run.add_argument("--show-thinking", action="store_true", help="stream the model's reasoning")
    run.add_argument("-y", "--yes", action="store_true", help="approve risky commands without asking")
    run.add_argument("--base", help="branch a new chat starts from and merges into (default: the current branch)")
    run.add_argument("--no-worktree", action="store_true", help="work directly in the directory, without a chat branch")
    run.add_argument("--plain-commits", action="store_true", help="do not ask the model to write commit messages")
    run.add_argument("-a", "--attach", action="append", default=[], metavar="FILE",
                     help="attach an image or document (repeatable): images are shown to vision models, "
                          "PDFs are converted to text")
    run.add_argument("--no-subtasks", action="store_true",
                     help="do not offer the Task tool (no sub-agents)")
    run.add_argument("--review", action="store_true",
                     help="after the task, have the reviewer role read the diff and fix what it finds")
    run.add_argument("--allow-cloud", action="store_true",
                     help="let the agent switch to cloud models marked switchable (set_model)")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="current branch, uncommitted changes and pending chat branches")
    status.add_argument("-w", "--work-dir", default=".")
    status.set_defaults(func=cmd_status)
    for name, func, text in (
        ("diff", cmd_diff, "show a chat branch's commits and changes"),
        ("merge", cmd_merge, "squash a chat branch into the branch it came from"),
        ("keep", cmd_keep, "keep a chat branch for later and remove its worktree folder"),
        ("discard", cmd_discard, "throw a chat branch away"),
    ):
        command = sub.add_parser(name, help=text)
        command.add_argument("chat", nargs="?", help="chat id, prefix or branch (default: the latest chat here)")
        command.add_argument("-w", "--work-dir", default=".")
        command.set_defaults(func=func)
    sub.choices["diff"].add_argument("--stat", action="store_true", help="only list the changed files")
    sub.choices["merge"].add_argument("--plain", action="store_true", help="do not ask the model for the commit message")
    sub.choices["discard"].add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    skills = sub.add_parser("skills", help="the instructions the model can open for a kind of work")
    ssub = skills.add_subparsers(dest="skills_command", required=True)
    slist = ssub.add_parser("list", help="skills available in a working directory")
    slist.set_defaults(func=cmd_skills_list)
    sshow = ssub.add_parser("show", help="print one skill as the model would read it")
    sshow.add_argument("name")
    sshow.set_defaults(func=cmd_skills_show)
    sadd = ssub.add_parser("add", help="copy a skill folder in (Claude Code's SKILL.md format)")
    sadd.add_argument("path", help="folder containing SKILL.md")
    sadd.add_argument("--project", action="store_true", help="add it to this project instead of every project")
    sadd.add_argument("--force", action="store_true", help="replace a skill of the same name")
    sadd.set_defaults(func=cmd_skills_add)
    sremove = ssub.add_parser("remove", help="delete a skill folder")
    sremove.add_argument("name")
    sremove.set_defaults(func=cmd_skills_remove)
    for command in (slist, sshow, sadd, sremove):
        command.add_argument("-w", "--work-dir", default=".")

    remote = sub.add_parser("remote", help="other machines: their named commands (start, stop, status…)")
    rsub = remote.add_subparsers(dest="remote_command", required=True)
    rsub.add_parser("list", help="machines and the commands they know").set_defaults(func=cmd_remote_list)
    rrun = rsub.add_parser("run", help="run one of a machine's named commands over ssh")
    rrun.add_argument("machine")
    rrun.add_argument("command_name", metavar="command")
    rrun.set_defaults(func=cmd_remote_run)

    web = sub.add_parser("web", help="the WebSearch and WebFetch tools: settings and a manual try")
    wsub = web.add_subparsers(dest="web_command", required=True)
    wsub.add_parser("show", help="print the web settings").set_defaults(func=cmd_web_show)
    wset = wsub.add_parser("set", help="change the web settings (keys go in with `la secret set`)")
    wset.add_argument("--search", choices=BACKENDS, help="where WebSearch gets results")
    wset.add_argument("--url", help="SearXNG's address (or another address for the same API)")
    wset.add_argument("--key-env", help="the secrets.env name of the search key (default BRAVE_API_KEY / TAVILY_API_KEY)")
    wset.add_argument("--enable", dest="enabled", action="store_true", default=None, help="give the agent the web tools")
    wset.add_argument("--disable", dest="enabled", action="store_false", help="no web tools (no internet here)")
    wset.set_defaults(func=cmd_web_set)
    wsearch = wsub.add_parser("search", help="search the way the agent would")
    wsearch.add_argument("query", nargs="+")
    wsearch.set_defaults(func=cmd_web_search)
    wfetch = wsub.add_parser("fetch", help="read a URL the way the agent would")
    wfetch.add_argument("url")
    wfetch.add_argument("--start", type=int, default=0)
    wfetch.set_defaults(func=cmd_web_fetch)

    chats = sub.add_parser("chats", help="list recent chats")
    chats.add_argument("-w", "--work-dir", default=".")
    chats.add_argument("--all", action="store_true", help="chats of every working directory")
    chats.add_argument("-n", "--limit", type=int, default=20)
    chats.set_defaults(func=cmd_chats)

    ui = sub.add_parser("ui", help="open the desktop window (or --browser)")
    ui.add_argument("-w", "--work-dir", default=None, help="folder to open first (default: the one opened last)")
    ui.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    ui.add_argument("--host", default="127.0.0.1", help="interface to listen on (default: only this computer)")
    ui.add_argument("--port", type=int, default=0, help="port (default: any free port)")
    ui.add_argument("--debug", action="store_true", help="enable the window's developer tools")
    ui.add_argument("--no-open", action="store_true", help="with --browser: only print the address")
    ui.set_defaults(func=cmd_ui)

    show = sub.add_parser("show", help="show the tasks and results of a chat")
    show.add_argument("chat", help="chat id or unique prefix")
    show.set_defaults(func=cmd_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    prepare_streams()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (RegistryError, git.GitError) as e:
        console.print(f"[red]{e}[/]")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
