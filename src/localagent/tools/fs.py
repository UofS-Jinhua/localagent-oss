"""File tools: Read, Write, Edit."""

import difflib
import re
from pathlib import Path

from localagent.attachments import IMAGE_TYPES, MAX_IMAGE_BYTES, data_url, pdf_text, render_pdf_page
from localagent.tools.base import Tool, ToolError, ToolOutput
from localagent.tools.workspace import Workspace, decode, is_binary

MAX_READ_LINES = 2000
DEFAULT_READ_LINES = 400
MAX_LINE_CHARS = 2000
MAX_LISTING = 200
_LINE_PREFIX = re.compile(r"^\s*\d+\t", re.MULTILINE)


def _numbered(lines: list[str], first: int) -> str:
    out = []
    for number, line in enumerate(lines, first):
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + f" [... {len(line) - MAX_LINE_CHARS} more chars]"
        out.append(f"{number:>6}\t{line}")
    return "\n".join(out)


def _suggest(path: Path) -> str:
    parent = path.parent
    while not parent.is_dir() and parent != parent.parent:
        parent = parent.parent
    try:
        names = [p.name for p in parent.iterdir()]
    except OSError:
        return ""
    close = difflib.get_close_matches(path.name, names, n=3, cutoff=0.5)
    return f" Similar names in {parent}: {', '.join(close)}." if close else ""


def _listing(ws: Workspace, path: Path) -> str:
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
    shown = entries[:MAX_LISTING]
    more = f"\n[... {len(entries) - MAX_LISTING} more entries]" if len(entries) > MAX_LISTING else ""
    body = "\n".join(shown) if shown else "(empty directory)"
    return f"{ws.display(path)} is a directory. Entries:\n{body}{more}"


def _existing_file(ws: Workspace, file_path: str) -> Path:
    path = ws.resolve(file_path)
    if path.is_dir():
        raise ToolError(f"{ws.display(path)} is a directory, not a file.")
    if not path.exists():
        raise ToolError(f"file not found: {ws.display(path)}.{_suggest(path)}")
    return path


def _writable(ws: Workspace, file_path: str) -> Path:
    path = ws.resolve(file_path)
    if not ws.contains(path):
        raise ToolError(f"{path} is outside the working directory {ws.root}; only files inside it may be changed.")
    return path


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _closest_block(text: str, old: str) -> str:
    """Describe the region of *text* most similar to *old*, to help the model retry."""
    lines = text.split("\n")
    size = max(1, old.count("\n") + 1)
    if len(lines) > 5000:
        return ""
    best, best_start = 0.0, 0
    target = old.strip()
    for start in range(0, max(1, len(lines) - size + 1)):
        ratio = difflib.SequenceMatcher(None, "\n".join(lines[start:start + size]).strip(), target).ratio()
        if ratio > best:
            best, best_start = ratio, start
    if best < 0.5:
        return ""
    block = _numbered(lines[best_start:best_start + size], best_start + 1)
    return f"\nThe most similar text is at lines {best_start + 1}-{best_start + size}:\n{block}"


def _match_ignoring_trailing_space(text: str, old: str) -> tuple[int, int] | None:
    """Find a unique block equal to *old* when trailing whitespace is ignored; return its char span."""
    lines = text.split("\n")
    wanted = [line.rstrip() for line in old.split("\n")]
    hits = [
        i for i in range(len(lines) - len(wanted) + 1)
        if all(lines[i + k].rstrip() == wanted[k] for k in range(len(wanted)))
    ]
    if len(hits) != 1:
        return None
    start = sum(len(line) + 1 for line in lines[:hits[0]])
    end = start + sum(len(line) + 1 for line in lines[hits[0]:hits[0] + len(wanted)]) - 1
    return start, end


def _snippet(text: str, index: int, new: str) -> str:
    lines = text.split("\n")
    first = _line_of(text, index)
    last = first + new.count("\n")
    lo, hi = max(1, first - 2), min(len(lines), last + 2)
    if hi - lo > 20:
        hi = lo + 20
    return _numbered(lines[lo - 1:hi], lo)


def make_fs_tools(ws: Workspace) -> list[Tool]:
    """Return Read, Write and Edit bound to *ws*."""

    def page(path: Path, text: str, start_line: int, max_lines: int, note: str = "") -> str:
        lines = text.splitlines()
        if not lines:
            return f"{ws.display(path)} is empty{note}."
        if start_line < 1 or start_line > len(lines):
            raise ToolError(f"start_line must be between 1 and {len(lines)} (the file has {len(lines)} lines).")
        max_lines = min(max(1, max_lines), MAX_READ_LINES)
        end = min(len(lines), start_line - 1 + max_lines)
        header = f"{ws.display(path)} (lines {start_line}-{end} of {len(lines)}{note})"
        footer = f"\n[{len(lines) - end} more lines; continue with start_line={end + 1}]" if end < len(lines) else ""
        return f"{header}\n{_numbered(lines[start_line - 1:end], start_line)}{footer}"

    def read(file_path: str, start_line: int = 1, max_lines: int = DEFAULT_READ_LINES,
             page_number: int = 0) -> str | ToolOutput:
        path = ws.resolve(file_path)
        if path.is_dir():
            return _listing(ws, path)
        path = _existing_file(ws, file_path)
        suffix = path.suffix.lower()
        if suffix in IMAGE_TYPES:
            size = path.stat().st_size
            if size > MAX_IMAGE_BYTES:
                return f"{ws.display(path)} is an image of {size:,} bytes, too large to show (limit {MAX_IMAGE_BYTES:,})."
            return ToolOutput(f"{ws.display(path)} is an image ({size:,} bytes).",
                              [data_url(path.read_bytes(), IMAGE_TYPES[suffix])])
        data = path.read_bytes()
        if suffix == ".pdf" or data.startswith(b"%PDF-"):
            if page_number:  # a page as a picture: figures, diagrams, layout
                try:
                    png, pages = render_pdf_page(path, page_number)
                except ImportError:
                    raise ToolError("pypdfium2 is not installed, so PDF pages cannot be rendered.") from None
                except ValueError as e:
                    raise ToolError(str(e)) from None
                except Exception as e:
                    raise ToolError(f"could not render page {page_number} of {ws.display(path)}: {e}") from None
                return ToolOutput(f"{ws.display(path)} page {page_number} of {pages}, as an image "
                                  f"(omit page_number for the text).", [data_url(png, "image/png")])
            try:
                text, pages = pdf_text(path)
            except Exception as e:
                raise ToolError(f"could not extract the text of {ws.display(path)}: {e}") from None
            return page(path, text, start_line, max_lines, f"; text extracted from a {pages}-page PDF")
        if is_binary(data):
            return f"{ws.display(path)} is a binary file ({len(data)} bytes) and cannot be shown as text."
        text, encoding = decode(data)
        ws.record(path, data, encoding)
        return page(path, text, start_line, max_lines)

    def write(file_path: str, content: str) -> str:
        path = _writable(ws, file_path)
        encoding = "utf-8"
        if path.is_dir():
            raise ToolError(f"{ws.display(path)} is a directory.")
        if path.exists():
            problem = ws.staleness(path)
            if problem:
                raise ToolError(f"{ws.display(path)} already exists. {problem} Use Edit to change existing files.")
            encoding = ws.encoding_of(path)
            if b"\r\n" in path.read_bytes():
                content = content.replace("\r\n", "\n").replace("\n", "\r\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode(encoding)
        path.write_bytes(data)
        ws.record(path, data, encoding)
        line_count = content.count("\n") + 1
        return f"Wrote {ws.display(path)} ({line_count} lines)."

    def edit(file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        _writable(ws, file_path)
        path = _existing_file(ws, file_path)
        if path.suffix.lower() in (".pdf", *IMAGE_TYPES):
            raise ToolError(f"{ws.display(path)} is not a text file; Edit only changes text files.")
        problem = ws.staleness(path)
        if problem:
            raise ToolError(f"cannot edit {ws.display(path)}: {problem}")
        if old_string == "":
            raise ToolError("old_string is empty; use Write to create a file.")
        if old_string == new_string:
            raise ToolError("old_string and new_string are identical; nothing would change.")
        text, encoding = decode(path.read_bytes())
        crlf = "\r\n" in text
        body = text.replace("\r\n", "\n")
        old = old_string.replace("\r\n", "\n")
        new = new_string.replace("\r\n", "\n")
        note = ""
        count = body.count(old)
        if count == 0 and _LINE_PREFIX.search(old):
            stripped = _LINE_PREFIX.sub("", old)
            if stripped and body.count(stripped) == 1:
                old, new = stripped, _LINE_PREFIX.sub("", new)
                count, note = 1, " (removed Read line-number prefixes from old_string)"
        if count == 0:
            span = _match_ignoring_trailing_space(body, old)
            if span is None:
                raise ToolError(
                    f"old_string was not found in {ws.display(path)}. It must match the file exactly "
                    f"(whitespace included, without Read's line-number prefixes).{_closest_block(body, old)}"
                )
            old, count, note = body[span[0]:span[1]], 1, " (matched ignoring trailing whitespace)"
        if count > 1 and not replace_all:
            where, start = [], body.find(old)
            while start != -1 and len(where) < 10:
                where.append(str(_line_of(body, start)))
                start = body.find(old, start + 1)
            raise ToolError(
                f"old_string occurs {count} times (lines {', '.join(where)}). Include more surrounding "
                "lines to make it unique, or set replace_all=true."
            )
        index = body.find(old)
        updated = body.replace(old, new) if replace_all else body.replace(old, new, 1)
        output = updated.replace("\n", "\r\n") if crlf else updated
        data = output.encode(encoding)
        path.write_bytes(data)
        ws.record(path, data, encoding)
        replaced = count if replace_all else 1
        return (
            f"Edited {ws.display(path)}: replaced {replaced} occurrence(s){note}.\n"
            f"{_snippet(updated, index, new)}"
        )

    return [
        Tool(
            "Read",
            "Read a text file. Returns lines prefixed with line numbers (number + tab). "
            "Reading a directory lists its entries. Use start_line/max_lines for large files. "
            "PDFs are returned as extracted text; images are shown to you if you can view images.",
            {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path relative to the working directory, or absolute."},
                    "start_line": {"type": "integer", "description": "1-based first line (default 1)."},
                    "max_lines": {"type": "integer", "description": f"Lines to return (default {DEFAULT_READ_LINES}, max {MAX_READ_LINES})."},
                    "page_number": {
                        "type": "integer",
                        "description": "PDFs only: show this page as an image instead of text, for figures and diagrams.",
                    },
                },
                "required": ["file_path"],
            },
            read,
        ),
        Tool(
            "Write",
            "Create a new file with the given content. Overwriting an existing file requires reading it "
            "first; prefer Edit for changes to existing files.",
            {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string", "description": "The complete file content."},
                },
                "required": ["file_path", "content"],
            },
            write,
        ),
        Tool(
            "Edit",
            "Replace an exact string in a file you have already read. old_string must match the file "
            "exactly, including indentation, and must not include the line-number prefixes shown by Read. "
            "It must be unique unless replace_all is true.",
            {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "old_string": {"type": "string", "description": "Exact text to replace."},
                    "new_string": {"type": "string", "description": "Replacement text."},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)."},
                },
                "required": ["file_path", "old_string", "new_string"],
            },
            edit,
        ),
    ]
