"""Skills: instructions for one kind of work, opened only when they are needed.

A skill is a folder with a ``SKILL.md`` in it: YAML front matter naming it and
saying when it applies, then Markdown telling the model how to do that kind of
work. Anything else in the folder (scripts, reference files, templates) is just
files the model can Read or run. The format is Claude Code's, so a skill written
for it usually works here unchanged -- what does not carry over is front matter
about tools it does not have here, and text written for a frontier model's
patience.

Where they come from, project first so a repository can override or add to what
the user keeps everywhere:

* ``<work dir>/.localagent/skills/<name>/SKILL.md`` -- travels with the project
* ``<data>/skills/<name>/SKILL.md`` -- every project
* ``<data>/synced-skills/<computer>/<name>/SKILL.md`` -- the user skills of a computer
  that logged in to this machine from its app (see ``skillsync``); a copy, replaced on every sync

``<data>`` is LocalAgent's own folder (``config.home_dir``): ``data/`` in the checkout, or ``$LOCALAGENT_HOME``.

**Only the catalogue is in the prompt.** The prefix (system prompt plus tool
schemas, ~2.3K tokens) has to stay byte-identical for the local server's prefix
cache to hold, and every token of it costs prefill on a cold start. A catalogue
line costs about 15 tokens and changes only when the user adds a skill; the body
arrives as a tool result, which is appended like any other and leaves the cached
prefix alone. Loading twenty skills into the prompt instead would cost minutes of
prefill and crowd out the task.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

from localagent.config import home_dir
from localagent.tools.base import Tool, ToolError

SKILL_FILE = "SKILL.md"
PROJECT_SKILLS = ".localagent/skills"
MAX_SKILLS = 50
MAX_DESCRIPTION_CHARS = 300
MAX_BODY_CHARS = 16000
FRONT_MATTER = "---"


@dataclass(frozen=True)
class Skill:
    """One skill: what it is for, and the instructions themselves."""

    name: str
    description: str
    body: str
    path: Path  # the SKILL.md file
    source: str  # "project", "user", or "synced" (copied from the computer that logged in here)

    @property
    def folder(self) -> Path:
        return self.path.parent


def user_skills_dir() -> Path:
    return home_dir() / "skills"


def synced_skills_dir() -> Path:
    return home_dir() / "synced-skills"


def project_skills_dir(root: Path) -> Path:
    return Path(root) / PROJECT_SKILLS


def _split_front_matter(text: str) -> tuple[dict, str]:
    """Return ``(front matter, body)``; a file without front matter is all body."""
    if not text.startswith(FRONT_MATTER):
        return {}, text
    end = text.find(f"\n{FRONT_MATTER}", len(FRONT_MATTER))
    if end == -1:
        return {}, text
    head, body = text[len(FRONT_MATTER):end], text[end + len(FRONT_MATTER) + 1:]
    try:
        data = yaml.safe_load(head)
    except yaml.YAMLError:
        return {}, text  # malformed: better to show the whole file than to lose it
    return (data if isinstance(data, dict) else {}), body.lstrip("\n")


def _first_sentence(body: str) -> str:
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line
    return ""


def read_skill(path: Path, source: str) -> Skill | None:
    """Parse one ``SKILL.md``. Returns ``None`` if it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    data, body = _split_front_matter(text)
    name = str(data.get("name") or path.parent.name).strip()
    description = str(data.get("description") or _first_sentence(body)).strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS].rstrip() + "…"
    if not name:
        return None
    return Skill(name=name, description=" ".join(description.split()), body=body.strip(),
                 path=path, source=source)


def _from_dir(directory: Path, source: str) -> list[Skill]:
    if not directory.is_dir():
        return []
    found = []
    for child in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        skill = read_skill(child / SKILL_FILE, source) if child.is_dir() else None
        if skill is not None:
            found.append(skill)
    return found


def load_skills(root: Path | str | None = None) -> list[Skill]:
    """Every available skill. Of two with one name the nearer wins: project, then user, then synced."""
    skills = _from_dir(project_skills_dir(Path(root)), "project") if root else []
    sources = [(user_skills_dir(), "user")]
    synced = synced_skills_dir()
    if synced.is_dir():
        sources += [(d, "synced") for d in sorted(synced.iterdir(), key=lambda p: p.name.lower())
                    if d.is_dir() and not d.name.startswith(".")]  # a dot folder is a sync still arriving
    for directory, source in sources:
        taken = {s.name for s in skills}
        skills += [s for s in _from_dir(directory, source) if s.name not in taken]
    return sorted(skills, key=lambda s: s.name.lower())[:MAX_SKILLS]


def catalogue(skills: list[Skill]) -> str:
    """The prompt block: what exists and how to open it. Never the bodies."""
    if not skills:
        return ""
    lines = "\n".join(f"- {s.name}: {s.description}" if s.description else f"- {s.name}" for s in skills)
    return (
        "\n<skills>\n"
        "Instructions the user has written for particular kinds of work. When one of these covers the "
        "task, call Skill(name) to read it before you start, and follow what it says.\n"
        f"{lines}\n"
        "</skills>\n"
    )


def skill_text(skill: Skill) -> str:
    """The body handed to the model, with a pointer to the skill's own files."""
    body = skill.body
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + f"\n[... {len(skill.body) - MAX_BODY_CHARS} more characters; " \
                                       f"read {skill.path} for the rest]"
    extras = sorted(p.name for p in skill.folder.iterdir() if p.name != SKILL_FILE) if skill.folder.is_dir() else []
    if extras:
        body += (f"\n\nFiles that belong to this skill are in {skill.folder} "
                 f"({', '.join(extras[:20])}). Read them from there; run them with Bash.")
    return body


def make_skill_tool(skills: list[Skill]) -> Tool:
    """The Skill tool: return one skill's instructions."""
    by_name = {s.name: s for s in skills}

    def open_skill(name: str) -> str:
        skill = by_name.get(name.strip())
        if skill is None:
            raise ToolError(f"no skill named {name!r}; available: {', '.join(by_name) or '(none)'}")
        return skill_text(skill)

    return Tool(
        "Skill",
        "Read the instructions of one of the skills listed in the system prompt. Do this before "
        "starting work the skill covers, and follow it.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": list(by_name), "description": "Which skill to read."},
            },
            "required": ["name"],
        },
        open_skill,
    )
