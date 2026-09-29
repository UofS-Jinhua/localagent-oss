# LocalAgent: notes for coding agents

- Start with `docs/architecture.md`: layers, module map, data flow, invariants, events. Read it instead of the whole codebase.
- KISS Sorcar (the reference design) is summarized in `docs/kiss-sorcar-notes.md`; do not re-read its source.
- Roadmap and measured model numbers: `docs/design.md` (§1 the server's speed and cache behaviour, §1.5 what a sub-agent costs and saves, §1.6 the on-demand model on an A100 server).
- Python only through the project venv: `.venv\Scripts\python.exe` (Windows) or `.venv/bin/python`. Never `pip install` globally.
- Tests: `.venv\Scripts\python -m pytest` (offline). Real model: `-m live` or `scripts/demo_fix_bug.py [--context-budget 5500] [--worktree]`, both using the registry in `$LOCALAGENT_HOME` (default `~/.localagent`).
- The suite must pass on Linux too, not only Windows: no bare `python` in test commands (use `sys.executable`), no reliance on the Chinese Windows code page.
- A new `.sh` committed from Windows lacks the executable bit: `git update-index --chmod=+x` it.
- `scripts/gpu-server/` runs on the GPU server, not in the app: after changing it, copy it to the server's work directory as well (README, "把模型放到有 GPU 的服务器上").
- Keep the invariants in `docs/architecture.md` §4, above all the append-only conversation and the stable system prompt.
- Tests exercise real behavior (real files, git, HTTP via `tests/scripted_server.py`); no mocks.
- Every `subprocess` call goes through `procutil.quiet()`; a test scans the source for new ones that do not.
- Changed `frontend/src`? Run `npm run build` in `frontend/` and commit the rebuilt `src/localagent/server/static` with it — that bundle is what ships.
- Docs under `docs/` and the README are written in Chinese; code, comments and commit messages are in English.
- When you change the architecture, update `docs/architecture.md` in the same commit.
- Everything outside `private/` is published as open source, commit subjects included: no personal names, accounts, host names, paths, IPs or model sources in code, tests, docs or commit messages. Tests use placeholders (`gpu1`, `alice@gpu2.example.edu`, `/srv/alice`); notes about a particular machine go in `private/`.
