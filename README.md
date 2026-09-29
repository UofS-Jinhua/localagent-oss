# LocalAgent

**English** | [中文](README.zh-CN.md)

A coding agent in the spirit of Claude Code and Codex, built for **local LLMs**: LM Studio, Ollama, llama.cpp, vLLM or anything else that speaks the OpenAI chat API, with cloud models as an option. It reads and edits code, runs commands and tests, and keeps every chat on its own git branch. Its layers follow [KISS Sorcar](https://github.com/ksenxx/kiss_ai).

Local models change the trade-offs. Prefill is slow, the server's prefix cache is what makes a long conversation affordable, context windows are small, and thinking models can think themselves into a corner. LocalAgent is designed around that:

- The conversation is **append-only** and the system prompt never changes, so each step reuses the server's cache instead of reprocessing the whole conversation.
- The system prompt and tool definitions stay around 2,300 tokens.
- When the window fills up, the model writes a handoff note and a fresh session carries on from it.
- Thinking has its own budget, separate from the output budget. A step that ends without a tool call is retried with less thinking before the task gives up.
- Sub-agents keep bulky tool output out of the main conversation. They save context, not time.

The measurements behind these choices are in [docs/design.md](docs/design.md).

## Features

- **Desktop app or browser.** A VS Code-style workbench: explorer, source control, Monaco for files and side-by-side diffs, a command palette (`Ctrl+P`, `Ctrl+Shift+P`). There is also a command line, `la`.
- **Tools:** Read, Write and Edit (a file must be read before it can be edited), Bash (Git Bash on Windows), Grep, Glob, RunTests, Plan, AskUser, WebSearch and WebFetch, Task (sub-agents), Skill and set_model.
- **A git branch per chat.** The agent works in a worktree on `la/<chat>` and never touches your checkout. Merge (as one squashed commit), keep or discard it from the app or the command line. Folders that are not git repositories work too.
- **Long tasks.** Handoff notes carry a task across context windows. A chat continues where the last task stopped. You can add instructions while a task runs, or stop it.
- **Reviewer.** Optionally, a read-only reviewer reads the diff and runs the tests before the commit. It can be a different model, even from another vendor. What it finds goes back to the main model, and the fixes land in the same commit.
- **Models.** Local servers and cloud presets: Gemini, OpenAI, Anthropic (through its OpenAI-compatible endpoint), DeepSeek, OpenRouter and Qwen (DashScope). API keys live in `data/secrets.env`, never in the model registry or in the environment of the agent's commands. A role set to `auto` uses whatever model LM Studio has loaded. Cost is counted per step. Cloud models are used only when you allow it.
- **Attachments.** Paste or drop screenshots and draw on them. PDFs are turned into text, and vision models can look at a page rendered as an image.
- **Skills.** Folders with a `SKILL.md`, in the same format as Claude Code. Only their names and one-line descriptions go into the prompt; the model opens one when it needs it.
- **Remote machines.** As with VS Code Remote-SSH, the agent runs on the server and the window stays on your computer. Log in from the app, jump hosts and password prompts included. On a shared server, forwarded ports are checked so that no other user can intercept them. Your skills are copied to the server.
- **GPU server scripts.** llama.cpp behind llama-swap, started on the first request and unloaded after 15 idle minutes, plus a private SearXNG for web search.

## Requirements

- Python 3.12 or newer ([python.org](https://www.python.org/downloads/); on Windows tick "Add python.exe to PATH"), and git.
- Windows 10 or 11 (the window uses WebView2), macOS or Linux. On Windows, Git for Windows: the Bash tool runs in Git Bash.
- A model: an OpenAI-compatible server such as LM Studio, or an API key for a cloud model.
- Node.js is **not** needed: the built UI is part of the repository.

## Install

**Windows:** double-click `setup.cmd`. It finds Python 3.12+, creates `.venv`, installs the dependencies and puts a **LocalAgent** shortcut in the project folder. The shortcut opens the window without a console.

```powershell
setup.cmd                                        # or double-click it
setup.cmd -BaseUrl http://<lm-studio-host>:1234  # also register your model server
setup.cmd -Dev                                   # include the test dependencies
setup.cmd -Desktop                               # also put the shortcut on the desktop and in the Start menu
setup.cmd -NoShortcut                            # no shortcut
```

Then open `LocalAgent.lnk`, or `LocalAgent.cmd`, which runs the setup first when `.venv` is missing. Run `setup.cmd` again after pulling new code.

**macOS / Linux:**

```bash
./scripts/setup.sh      # options: --base-url http://host:1234, --dev, --headless (a server without a desktop)
.venv/bin/la ui
```

**By hand:**

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev,desktop]"   # Windows: .venv\Scripts\python
```

## Quick start

The commands below use `la`: that is `.venv\Scripts\la` on Windows and `.venv/bin/la` elsewhere, unless the venv is activated.

```bash
# 1. Register your model server. LM Studio is recognized, and its context lengths are read.
la init --base-url http://<host>:1234

# 2. List the models and probe one: tool calls, prefill speed, prefix cache, generation speed, thinking on/off.
la models list
la models test

# 3. Open the app on a project (add --browser to use your browser instead of a window)...
la ui -w path/to/project

# ...or run a task from the terminal.
la run -w path/to/project "The tests fail. Find the bug and fix it without changing the tests."
```

Useful options of `la run`:

| Option | Effect |
|---|---|
| `-c`, `--chat ID` | continue the latest chat of this folder, or the given one |
| `-a FILE` | attach an image or a document (repeatable) |
| `--review` | have the reviewer read the diff and fix what it finds |
| `--scratch` | work in a scratch folder instead of a project |
| `--allow-cloud` | let the agent switch to cloud models you marked as switchable |
| `--no-thinking`, `--show-thinking` | turn thinking off, or stream it |
| `--no-subtasks` | no sub-agents |
| `-y` | do not ask before risky commands (`git push`, `rm -rf`, ...) |
| `-m MODEL`, `--base BRANCH`, `--no-worktree` | pick the model, the branch to start from, or work without a chat branch |

Chat branches from the command line: `la status`, `la diff`, `la merge`, `la keep`, `la discard`. Past chats: `la chats`, `la show ID`. Also `la secret`, `la web`, `la skills` and `la remote`; `la <command> --help` explains each.

**Cloud models:**

```bash
la secret set GEMINI_API_KEY                         # asks for the key without showing it
la init --preset gemini --only gemini-flash-latest
la models test gemini-flash-latest --quick           # one or two requests
```

Or in the app: the gear at the bottom left, then Models.

## Where things are kept

Everything the app writes goes into `data/` in the checkout: the model registry (`models.yaml`), API keys (`secrets.env`), the chat database, attachments, the scratch folder, and the settings for remote machines, web search and skills. `data/` is ignored by git. Set `LOCALAGENT_HOME` to keep it somewhere else; an installed wheel uses `~/.localagent`.

## Servers and GPUs

[docs/servers.md](docs/servers.md) (in Chinese) explains how to:

- work on a remote server from the app, or by hand over `ssh -L`;
- push the code to a server that has no GitHub credentials;
- run a model on a shared GPU server only while it is used (llama.cpp and llama-swap, with the scripts in [scripts/gpu-server/](scripts/gpu-server/));
- run SearXNG for web search, and manage all of these services with one command.

## Documentation

These are written in Chinese:

- [docs/architecture.md](docs/architecture.md): layers, module map, data flow, invariants and events. Read it before changing the code.
- [docs/design.md](docs/design.md): the design, the measurements behind it, and the milestones.
- [docs/changelog.md](docs/changelog.md): what changed and why, with measurements, newest first.
- [docs/kiss-sorcar-notes.md](docs/kiss-sorcar-notes.md): notes on KISS Sorcar, the reference design.

## Development

```bash
.venv/bin/python -m pytest            # offline: a scripted local HTTP server stands in for the model
.venv/bin/python -m pytest -m live    # end to end, against a real model from your registry
```

The tests pass on Windows and on Linux. The UI is in [frontend/](frontend/) (React, TypeScript, Vite). After changing `frontend/src`, run `npm install` and `npm run build` in `frontend/`, and commit the rebuilt `src/localagent/server/static` with it. Notes for coding agents, which apply to people too: [CLAUDE.md](CLAUDE.md).

## Roadmap

- Local and cloud models working on one task in parallel.
- Suggested next steps when a task ends.
- Continuing from any earlier task, and completion for commands and tasks.
- A spending cap for cloud models.
- MCP servers, behind a per-project allowlist: every server's tools cost prompt tokens, and small models pick the wrong tool more often once there are 30 or more.

Deliberately left out: an agent that writes its own memory for each project. KISS Sorcar found that this lowers quality, because the remembered facts go stale and small models follow stale notes more readily than frontier models do. Put lasting rules in the project's own `CLAUDE.md` or `AGENTS.md`, which the agent reads on every task.

## License

[Apache License 2.0](LICENSE). The third-party components bundled with the UI (Monaco, Codicons, React and others) are listed in [NOTICE](NOTICE), with their license texts in [THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt).
