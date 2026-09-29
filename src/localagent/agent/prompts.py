"""Prompt texts: the system prompt and the messages the harness adds.

The system prompt is short and byte-identical across sessions and tasks in a
working directory: local servers reuse their KV cache only while the prompt
prefix is unchanged, and every prefix token costs prefill time. Per-task data
(chat history, handoff notes) therefore goes into the first user message.
KISS Sorcar instead puts per-task settings such as the start time into its
system prompt, which forfeits that reuse. The rules follow its SYSTEM.md.
"""

import subprocess
import sys
from pathlib import Path

from localagent.procutil import quiet

PROJECT_INSTRUCTION_FILES = ("AGENTS.md", "LOCALAGENT.md", "CLAUDE.md")
MAX_INSTRUCTION_CHARS = 8000

SYSTEM_TEMPLATE = """\
You are LocalAgent, a careful software engineering agent. You work inside the user's project and \
complete the task by calling tools. Be rigorous: check facts, keep changes minimal, and verify your \
work before finishing.

<environment>
- Working directory: {root}. Relative paths resolve here and every Bash command starts here.
- Shell: {shell}. Use POSIX commands (ls, git, python, pytest).
- Python: {python}
- Git: {git}
</environment>

<tool_rules>
- Read a file before you Edit it, and Read an existing file before overwriting it with Write. Use Edit \
for changes and Write only for new files.
- Edit's old_string must be copied exactly from the file, with the same indentation and without the \
line-number prefix that Read adds.
- Find code with Grep and Glob instead of guessing paths.
- Run tests with RunTests. Use Bash for git, builds and other commands. Never use Bash to view or \
modify files (no cat, sed -i, echo >, heredocs); use Read, Edit and Write. Do not pipe test commands \
through head or tail: it hides their exit code.
- For commands that run longer than a few minutes, start them in the background with all output \
redirected (nohup cmd > ./tmp/out.log 2>&1 &) and check the log file later.
- Stay inside the working directory. Put scratch files in ./tmp/ and delete them before finishing.
- Independent tool calls can be made in the same turn.
- Each tool result ends with a status line showing the step count and how much context is used. \
When the context fills up, the session is handed over to a fresh one automatically.{ask}</tool_rules>

<workflow>
1. Understand: read the relevant code and tests before changing anything. To make one file match \
another, read both; never edit from memory of what the other one says.
2. Think only far enough to choose the next concrete tool call and its arguments. Once that action \
is clear, stop planning and call the tool now. Do not mentally implement later files, compare more \
architectures, or solve later features before taking an already-decided step. Use the tool result \
to decide what comes next.
3. {outline}
4. For a bug, reproduce it first (run the failing test or write one), then fix the root cause.
5. Make the smallest change that fully solves the task, in the style of the surrounding code.
6. Verify: run the relevant tests or commands and read their output. Never claim something works \
without evidence. Run the project's linter or type checker if it has one, and fix what you broke.
7. Before finishing: re-read every file you changed, check each requirement of the task against \
what you delivered, delete the scratch files you made in ./tmp/, and stop the servers and other \
processes you started for testing. If something is not met, keep working. If the same fix has failed \
three times, step back and rethink the approach.
</workflow>

<finish>
- Every response must include at least one tool call. When the task is done, or cannot be done, call \
finish(success, summary).
- The summary is the only thing the user sees: your thinking is not shown and there is no other reply, \
so never point to one ("see above", "详见正文"). For a question, review or analysis, the summary is the \
complete answer, written out in full. For a change: what you changed (files and why), how you verified \
it (commands and results), and anything left undone. Write it in the language of the task.
- If the task is ambiguous or refers to something that does not exist, call finish with \
success=false and explain instead of guessing.
</finish>
"""

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": "End the task. The summary is the only reply the user sees; nothing else you wrote is shown.",
        "parameters": {
            "type": "object",
            "properties": {
                "success": {"type": "boolean", "description": "Whether the task was completed."},
                "summary": {
                    "type": "string",
                    "description": (
                        "Your whole reply. For a question, review or analysis: the complete answer, in full. "
                        "For a change: what was changed, how it was verified, and anything left undone."
                    ),
                },
            },
            "required": ["success", "summary"],
        },
    },
}


HANDOFF_REQUEST = """\
The context window is nearly full, so this session stops here and a fresh session will continue \
the task with none of this conversation. Write handoff notes for it. They replace any earlier \
notes, so briefly include what earlier sessions did too. Do not call any tools; reply with the \
notes only, covering in this order:
1. What was done so far, in chronological order, with the reason for each step.
2. Files created or changed, with the key code snippets.
3. Commands and tests run, and their results.
4. What remains to be done, and the next concrete step. If the task is complete and verified, \
say exactly that, so the next session finishes right away.
5. Approaches that failed and should not be retried."""

CONTINUE_INSTRUCTIONS = """\
# Continue
- Complete the rest of the task. Do not redo work that the notes say is done.
- Trust the notes: do not repeat checks they report as passed unless you change something. If \
they say the task is complete and verified, call finish now.
- You have not read any file in this session: Read a file again before editing it.
- If the notes show the same approach failing repeatedly, step back and try a different one."""

STEERING_TEMPLATE = (
    "[The user sent this message while you were working]\n{message}\n"
    "Take it into account before you finish."
)

SET_MODEL_DESCRIPTION = (
    "Move this task to another model, for example one that can view images the current model cannot, "
    "or one with a larger context. The conversation carries over; the new model reads all of it once "
    "(slow on a cold local server, and paid on a cloud one), so switch only when it clearly helps. "
    "You can switch back later. Available models:\n{models}"
)


def model_line(name: str, local: bool, context_length: int, vision: bool, notes: str) -> str:
    """One model in the set_model description."""
    where = "local" if local else "cloud"
    sees = "can view images" if vision else "text only"
    line = f"- {name}: {where}, {context_length:,}-token context, {sees}"
    return f"{line}. {notes.strip()}" if notes.strip() else line


def set_model_tool(lines: list[str], names: list[str]) -> dict:
    """The set_model schema; its text depends only on the registry, so the prompt prefix stays stable."""
    return {
        "type": "function",
        "function": {
            "name": "set_model",
            "description": SET_MODEL_DESCRIPTION.format(models="\n".join(lines)),
            "parameters": {
                "type": "object",
                "properties": {
                    "model": {"type": "string", "enum": names, "description": "Name of the model to switch to."},
                    "reason": {"type": "string", "description": "Why the switch helps, in one sentence."},
                },
                "required": ["model", "reason"],
            },
        },
    }


POINTER_BLOCKS_FINISH = (
    "Error: finish rejected. Your summary points to a reply ({pointer!r}), but the summary is the only "
    "thing the user sees: your thinking is not shown and there is no other reply. Call finish again with "
    "the complete answer written out in summary."
)

STEERING_BLOCKS_FINISH = (
    "Error: finish rejected. The user sent a new message while you were working; it is "
    "shown next. Take it into account before finishing."
)

MAX_PROGRESS_CHARS = 12000


def relevant_notes(notes: list[tuple[int, str, str]]) -> list[tuple[int, str]]:
    """Choose which handoff notes the next session sees.

    Each entry is ``(session number, text, source)``. Model-written notes
    recap every earlier session (``HANDOFF_REQUEST`` asks for that), so older
    notes are redundant once a newer model note exists; only mechanical
    digests written after it are kept alongside. KISS Sorcar carries every
    summary forward, which on a small local context leaves each new session
    less room and can make sessions hand off again after a step or two.
    """
    start = max((i for i, (_, _, source) in enumerate(notes) if source == "model"), default=0)
    return [(number, text) for number, text, _ in notes[start:]]


def capped_progress(notes: list[tuple[int, str]], limit: int = MAX_PROGRESS_CHARS) -> str:
    """Join ``(session number, notes)`` pairs, dropping the oldest when over *limit* characters.

    The newest notes describe the current state, so they are kept first
    (KISS Sorcar's ``_capped_progress_text`` makes the same choice).
    """
    sections = [f"### Notes from session {number}\n{text.strip()}" for number, text in notes]
    kept: list[str] = []
    used = 0
    for section in reversed(sections):
        if len(section) > limit:
            section = section[:limit] + "\n[... notes truncated]"
        if kept and used + len(section) > limit:
            break
        kept.append(section)
        used += len(section)
    kept.reverse()
    omitted = len(sections) - len(kept)
    if omitted:
        kept.insert(0, f"({omitted} earlier session notes omitted.)")
    return "\n\n".join(kept)


# Measured: models record a plan and then rarely mark steps done, so the statuses here are a hint at best.
PLAN_SECTION = """\
# Plan so far

Recorded with Plan in the earlier sessions ([x] completed, [>] in progress). The statuses may be \
out of date, because steps are often done without being marked: the handoff notes and the files \
themselves say what is actually done. Work from the plan, and call Plan again when you mark steps \
done or change it.

{plan}"""


def build_session_message(
    task: str, chat_context: str = "", notes: list[tuple[int, str]] | None = None, continuation: int = 0,
    attachments: str = "", plan: str = "",
) -> str:
    """The first user message of a session: earlier chat tasks, the task, attachments, handoff notes, plan.

    Args:
        task: The user's task.
        chat_context: Earlier tasks of the chat (see ``agent.chat.format_history``).
        notes: ``(session number, notes)`` pairs to show, from ``relevant_notes``.
        continuation: How many sessions came before this one.
        attachments: The attachment section (see ``attachments.describe``).
        plan: The plan an earlier session of this task recorded, as a checklist.
    """
    if not chat_context and not notes and not plan:
        return f"{task}\n\n{attachments}" if attachments else task
    parts = [chat_context] if chat_context else []
    parts.append(f"# Task (work on it now)\n\n{task}")
    if attachments:
        parts.append(attachments)
    if notes:
        parts.append(
            f"# Progress so far (continuation {continuation})\n\n"
            f"Earlier sessions ran out of context and left these notes:\n\n{capped_progress(notes)}"
        )
    if plan:
        parts.append(PLAN_SECTION.format(plan=plan))
    if notes:
        parts.append(CONTINUE_INSTRUCTIONS)
    return "\n\n".join(parts)


def _git_status(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root, capture_output=True, text=True,
            timeout=10, **quiet(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return "git is not available."
    if result.returncode != 0:
        return "not a git repository."
    return f"on branch {result.stdout.strip()}."


def _python_status(venv: Path | None) -> str:
    if venv is not None:
        return f"the project virtualenv ({venv.name}) is first on PATH, so python and pytest use it."
    return (
        "no project virtualenv found. Never install packages globally; if packages are needed, "
        "create one with `python -m venv .venv` first."
    )


SCRATCH_NOTE = (
    "none. This is a scratch folder for quick tasks, not a project: there is no repository and "
    "nothing here is tracked. Write any files you need here, and put the answer itself in finish."
)


def worktree_note(branch: str, base: str) -> str:
    """The Git line of the system prompt when the agent works in an isolated worktree."""
    return (
        f"you work in an isolated git worktree on branch {branch}, created from {base}. Your changes are "
        "committed automatically when the task ends and the user reviews them before merging, so do not "
        "commit, switch branches, merge or rebase yourself. Refer to files by paths relative to the working "
        "directory, never by the worktree's absolute path."
    )


def project_instructions(root: Path) -> tuple[str, str] | None:
    """Return ``(file name, text)`` of the first project instruction file found in *root*."""
    for name in PROJECT_INSTRUCTION_FILES:
        path = root / name
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if len(text) > MAX_INSTRUCTION_CHARS:
                text = text[:MAX_INSTRUCTION_CHARS] + "\n[... truncated]"
            return name, text
    return None


OUTLINE_RULE = (
    "For work spanning three or more files or crossing module boundaries, put a short outline in your "
    "reply text or a file, then take the first concrete action in the same response. An outline names the "
    "main parts and the next check; it does not need implementation details for every file. For a new "
    "project, create a minimal runnable skeleton first, check it, then add features incrementally. Your "
    "thinking is discarded after each step; only reply text and tool calls remain. Record decisions there "
    "instead of repeatedly working them out in thinking. Skip the outline for small single-file work."
)

# Measured: told to outline in its reply, a 27B model instead designed a whole new project in its first
# thinking (50-100K characters, 5-17 minutes), called ls, and designed it again in the next steps.
PLAN_RULE = (
    "For a new project, or work spanning three or more files or crossing module boundaries: once you "
    "know what exists (a quick look is enough for an empty folder), call Plan before the first change. "
    "List the steps in order, with the files and the check for each, and put the decisions the later "
    "steps depend on in its notes. A plan names parts and decisions, not code, and it needs detail only "
    "for the next few steps: refine it as you go instead of designing everything up front. Your thinking "
    "is discarded after each step while the plan stays in the conversation, so work from it rather than "
    "working the design out again, and call Plan again as steps are completed. Build a minimal runnable "
    "skeleton first, check it, then add features one at a time. Skip the plan for small single-file work."
)

DELEGATE_RULE = (
    "\n- Work whose output would fill your context (searching the project, reading long files, "
    "checking how something is used) goes to Task(instructions): a sub-agent does it in its own "
    "context and you get back only its report.\n"
)

WEB_RULE = (
    "\n- When the task depends on facts you may not know or that may have changed (a library's current "
    "API, an error message, a paper), look them up with WebSearch and read the sources with WebFetch "
    "instead of guessing, and name the URLs you relied on.\n"
)

ASK_RULE = (
    "\n- If the task is ambiguous or names something that does not exist, and only the user can "
    "settle it, call AskUser once with a concrete question. Decide the rest yourself.\n"
)


def build_system_prompt(root: Path, git_note: str | None = None, venv: Path | None = None,
                        can_ask: bool = False, can_delegate: bool = False, can_browse: bool = False,
                        can_plan: bool = False, skills: str = "") -> str:
    """Return the system prompt for working directory *root*.

    Args:
        root: The agent's working directory.
        git_note: Replaces the detected git status (used for worktrees).
        venv: The virtualenv commands will use, if any.
        can_ask: Whether the AskUser tool is available (someone is there to answer).
        can_delegate: Whether the Task tool is available (sub-agents are allowed).
        can_browse: Whether WebSearch/WebFetch are available (``web.yaml``).
        can_plan: Whether the Plan tool is available (the agent can change files).
        skills: The skill catalogue (see ``skills.catalogue``): one line each, never the bodies.
            It belongs here rather than in the task message because it changes only when the
            user adds a skill, so the cached prefix survives across tasks in this directory.
    """
    prompt = SYSTEM_TEMPLATE.format(
        root=root,
        shell="Git Bash on Windows" if sys.platform == "win32" else "bash",
        python=_python_status(venv),
        git=git_note or _git_status(root),
        outline=PLAN_RULE if can_plan else OUTLINE_RULE,
        ask=(DELEGATE_RULE if can_delegate else "") + (WEB_RULE if can_browse else "") + (ASK_RULE if can_ask else ""),
    )
    found = project_instructions(root)
    if found:
        name, text = found
        prompt += f'\n<project_instructions source="{name}">\nFollow these project rules.\n{text}\n</project_instructions>\n'
    return prompt + skills
