"""The AskUser tool: pause and ask the user when only they can answer.

KISS Sorcar has the same tool for a good reason: without it an agent that hits a
genuine ambiguity either guesses (and wastes a whole task) or gives up. It is
only registered when someone is actually there to answer (the desktop UI, or an
interactive terminal), so an unattended run never waits on a question nobody
will see.
"""

from collections.abc import Callable

from localagent.tools.base import Tool, ToolError

#: ``(question, options) -> answer``; an empty answer means nobody replied.
Asker = Callable[[str, list[str]], str]

MAX_QUESTIONS = 5
NO_ANSWER = (
    "No answer (the user may be away). Decide yourself, continue, and say in your finish "
    "summary what you assumed."
)
TOO_MANY = (
    "Error: you have already asked the user {limit} questions in this task. Decide yourself, "
    "continue, and state your assumptions in the finish summary."
)


def make_ask_tool(asker: Asker, limit: int = MAX_QUESTIONS) -> Tool:
    """Return the AskUser tool, which blocks until the user answers or does not."""
    asked = 0

    def ask_user(question: str, options: list[str] | None = None) -> str:
        nonlocal asked
        text = question.strip()
        if not text:
            raise ToolError("question must not be empty")
        if asked >= limit:
            return TOO_MANY.format(limit=limit)
        asked += 1
        choices = [str(o) for o in options or [] if str(o).strip()]
        answer = (asker(text, choices) or "").strip()
        return f"The user answered: {answer}" if answer else NO_ANSWER

    return Tool(
        "AskUser",
        "Ask the user a question and wait for their answer. Use it only when the task cannot be "
        "done correctly without their decision (which of several files they meant, a missing "
        "credential, a choice between two designs). Do not use it for things you can find out "
        "yourself by reading the project.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "One clear question, with the context needed to answer it."},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional choices to offer; the user may still answer freely.",
                },
            },
            "required": ["question"],
        },
        ask_user,
    )
