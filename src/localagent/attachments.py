"""Files the user attaches to a task: images the model looks at, documents it reads.

Attachments are stored once, content-addressed, under ``~/.localagent/attachments``
(so the UI can show them again and later tasks of the chat can refer to them),
and placed into the agent's workspace under ``.la-attachments/`` so the model
reaches them with its ordinary tools. That folder ignores itself for git, and is
junk for worktree commits, so attachments never end up in the user's history.

How the model receives them (see ``describe`` and ``user_content``):
- images: inline ``image_url`` parts after the task text (models without
  vision get a note instead, from ``models.openai_compat.adapt_messages``);
- PDFs: text extracted to ``<name>.txt`` next to the file, so Read and Grep work;
- small text documents (and small PDFs): their text is included in the message;
  larger ones are only referenced, because the opening message is repeated in
  every session and a local context window is small.
"""

import base64
import hashlib
import io
import json
import logging
import mimetypes
import re
import shutil
import struct
import zlib
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from localagent.config import home_dir
from localagent.fsutil import remove_tree

logger = logging.getLogger(__name__)

ATTACHMENT_DIR = ".la-attachments"
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # larger images are placed as files but not sent inline
PAGE_MAX_EDGE = 1400  # a rendered PDF page: enough to read a figure, not enough to flood the context
INLINE_TEXT_CHARS = 8000
_ID = re.compile(r"^[0-9a-f]{24}$")
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class Attachment:
    """A stored attachment (metadata only; the bytes live in the store)."""

    id: str
    name: str
    mime: str
    kind: str  # "image", "pdf", "text" or "file"
    size: int
    pages: int = 0
    chars: int = 0
    lines: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def safe_name(name: str) -> str:
    """A file name that is valid on every platform and cannot leave its folder."""
    base = _UNSAFE.sub("_", Path(name.replace("\\", "/")).name).strip(" .") or "attachment"
    stem, suffix = Path(base).stem[:80], Path(base).suffix[:12]
    return (stem or "attachment") + suffix


def _looks_like_text(data: bytes) -> bool:
    if b"\x00" in data[:8192]:
        return False
    try:
        data[:65536].decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        return e.start > 65530  # a multi-byte character cut at the sample's end


def kind_of(name: str, data: bytes, mime: str = "") -> tuple[str, str]:
    """``(kind, mime type)`` of an attachment from its name and bytes."""
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_TYPES or mime in IMAGE_TYPES.values():
        return "image", IMAGE_TYPES.get(suffix, mime)
    if suffix == ".pdf" or data.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    guessed = mime or mimetypes.guess_type(name)[0] or ""
    if _looks_like_text(data):
        return "text", guessed if guessed.startswith("text/") or guessed.endswith(("json", "xml")) else "text/plain"
    return "file", guessed or "application/octet-stream"


def extract_pdf(data: bytes) -> tuple[str, int]:
    """``(text, pages)`` of a PDF, with a ``--- page N ---`` line before each page."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts = []
    for number, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception as e:  # a broken page must not lose the rest of the document
            text = f"[text of this page could not be extracted: {e}]"
        parts.append(f"--- page {number} ---\n{text.strip()}")
    return "\n\n".join(parts) + "\n", len(reader.pages)


PNG_SIGNATURE = bytes([137, 80, 78, 71, 13, 10, 26, 10])


def _png(width: int, height: int, channels: int, buffer: bytes, stride: int) -> bytes:
    """Encode raw RGB/RGBA rows as a PNG (no imaging library needed).

    Every row is prefixed with filter byte 0, and *stride* can exceed the row
    itself because the renderer pads rows.
    """
    row_bytes = width * channels
    filter_byte = bytes(1)
    raw = b"".join(filter_byte + buffer[y * stride: y * stride + row_bytes] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    colour = 6 if channels == 4 else 2  # RGBA or RGB
    header = struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0)
    return (PNG_SIGNATURE + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def render_pdf_page(path: Path, page_number: int, max_edge: int = PAGE_MAX_EDGE) -> tuple[bytes, int]:
    """Render one page of a PDF as a PNG, for the figures the extracted text cannot carry.

    Returns ``(png bytes, page count)``; raises ``ValueError`` for a page that does not exist.
    """
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        count = len(document)
        if not 1 <= page_number <= count:
            raise ValueError(f"{path.name} has {count} page(s); page {page_number} does not exist")
        page = document[page_number - 1]
        width, height = page.get_size()  # in points
        scale = min(max_edge / max(width, height), 300 / 72)  # fit the cap, never above 300 dpi
        bitmap = page.render(scale=scale, rev_byteorder=True)  # rev_byteorder: RGB(A) instead of BGR(A)
        return _png(bitmap.width, bitmap.height, bitmap.n_channels, bytes(bitmap.buffer), bitmap.stride), count
    finally:
        document.close()


@lru_cache(maxsize=16)
def _pdf_text_cached(path: str, mtime_ns: int, size: int) -> tuple[str, int]:
    return extract_pdf(Path(path).read_bytes())


def pdf_text(path: Path) -> tuple[str, int]:
    """Extract (and remember) the text of a PDF file in the workspace."""
    stat = path.stat()
    return _pdf_text_cached(str(path), stat.st_mtime_ns, stat.st_size)


def data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


class AttachmentStore:
    """Content-addressed attachment storage: ``<root>/<id>/{content,meta.json,text.txt}``."""

    def __init__(self, root: Path | None = None):
        self.root = root or home_dir() / "attachments"

    def add(self, data: bytes, name: str, mime: str = "") -> Attachment:
        """Store *data* under *name* and return its metadata (the same bytes and name give the same id)."""
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"{name} is larger than {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB")
        if not data:
            raise ValueError(f"{name} is empty")
        name = safe_name(name)
        attachment_id = hashlib.sha256(name.encode() + b"\0" + data).hexdigest()[:24]
        folder = self.root / attachment_id
        if (folder / "meta.json").is_file():
            return self.get(attachment_id)
        kind, mime = kind_of(name, data, mime)
        attachment = Attachment(id=attachment_id, name=name, mime=mime, kind=kind, size=len(data))
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "content").write_bytes(data)
        text = None
        if kind == "pdf":
            try:
                text, attachment.pages = extract_pdf(data)
            except Exception as e:
                logger.warning("could not read PDF %s: %s", name, e)
        elif kind == "text":
            text = data.decode("utf-8", "replace")
        if text is not None:
            (folder / "text.txt").write_text(text, encoding="utf-8")
            attachment.chars, attachment.lines = len(text), text.count("\n") + 1
        (folder / "meta.json").write_text(json.dumps(attachment.to_dict()), encoding="utf-8")
        return attachment

    def add_file(self, path: str | Path) -> Attachment:
        path = Path(path)
        return self.add(path.read_bytes(), path.name)

    def get(self, attachment_id: str) -> Attachment:
        """Metadata of a stored attachment; ``KeyError`` if there is none."""
        if not _ID.match(attachment_id or ""):
            raise KeyError(attachment_id)
        try:
            meta = json.loads((self.root / attachment_id / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise KeyError(attachment_id) from None
        return Attachment(**meta)

    def content_path(self, attachment: Attachment) -> Path:
        return self.root / attachment.id / "content"

    def remove(self, attachment_id: str) -> None:
        """Delete a stored attachment; the caller makes sure no task refers to it any more."""
        if _ID.match(attachment_id or ""):  # an id, never a path that could climb out of root
            remove_tree(self.root / attachment_id)

    def text(self, attachment: Attachment) -> str | None:
        """The document's text (decoded or extracted), if it has any."""
        try:
            return (self.root / attachment.id / "text.txt").read_text(encoding="utf-8")
        except OSError:
            return None


@dataclass
class Placed:
    """An attachment copied into the workspace, with what goes into the model's message."""

    attachment: Attachment
    path: str  # relative to the workspace root, e.g. ".la-attachments/shot.png"
    text_path: str = ""  # extracted text of a PDF
    inline: str = ""  # a small document's full text
    data_url: str = ""  # an image, sent inline


def _same_file(path: Path, source: Path) -> bool:
    return path.is_file() and path.stat().st_size == source.stat().st_size and path.read_bytes() == source.read_bytes()


def place(store: AttachmentStore, attachments: list[Attachment], root: Path,
          inline: set[str] | None = None) -> list[Placed]:
    """Copy *attachments* into ``root/.la-attachments`` (in order, so names are stable across tasks).

    Args:
        inline: Ids whose image data and small-document text go into the message
            (default: all). Attachments of earlier tasks are only referenced.
    """
    folder = root / ATTACHMENT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.is_file():
        ignore.write_text("# attachments for LocalAgent tasks; never committed\n*\n", encoding="utf-8")
    placed = []
    taken: set[str] = set()
    for attachment in attachments:
        source = store.content_path(attachment)
        name = attachment.name
        target = folder / name
        if name in taken or (target.exists() and not _same_file(target, source)):
            name = f"{Path(name).stem}-{attachment.id[:6]}{Path(name).suffix}"
            target = folder / name
        taken.add(name)
        if not _same_file(target, source):
            shutil.copyfile(source, target)
        entry = Placed(attachment, f"{ATTACHMENT_DIR}/{name}")
        text = store.text(attachment) if attachment.kind in ("pdf", "text") else None
        if attachment.kind == "pdf" and text is not None:
            text_target = folder / f"{name}.txt"
            if not text_target.is_file():
                text_target.write_text(text, encoding="utf-8")
            entry.text_path = f"{ATTACHMENT_DIR}/{name}.txt"
        wanted = inline is None or attachment.id in inline
        if wanted and text is not None and len(text) <= INLINE_TEXT_CHARS:
            entry.inline = text
        if wanted and attachment.kind == "image" and attachment.size <= MAX_IMAGE_BYTES:
            entry.data_url = data_url(source.read_bytes(), attachment.mime)
        placed.append(entry)
    return placed


def unplace(root: Path, paths: set[str]) -> int:
    """Delete the copies *paths* (as ``place`` names them) from ``root/.la-attachments``; return how many.

    Only plain file names directly in that folder are touched, never a path that climbs out of it. The
    folder itself goes once nothing but its ``.gitignore`` is left, so the project is as it was before.
    """
    folder = Path(root) / ATTACHMENT_DIR
    if not folder.is_dir():
        return 0
    removed = 0
    for path in paths:
        prefix, _, name = path.partition("/")
        if prefix != ATTACHMENT_DIR or name in ("", ".", "..", ".gitignore") or "/" in name or "\\" in name:
            continue
        target = folder / name
        if target.is_file():
            target.unlink()
            removed += 1
    if {p.name for p in folder.iterdir()} <= {".gitignore"}:
        remove_tree(folder)
    return removed


def _size(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def _line(entry: Placed) -> str:
    a = entry.attachment
    if a.kind == "image":
        if entry.data_url:
            return f"- {entry.path}: image, shown after this message."
        return f"- {entry.path}: image ({_size(a.size)}), too large to show inline."
    if a.kind == "pdf":
        if not entry.text_path:
            return f"- {entry.path}: PDF, {_size(a.size)}; its text could not be extracted."
        if entry.inline:
            return f"- {entry.path}: PDF, {a.pages} page(s); its text is included below."
        return (f"- {entry.path}: PDF, {a.pages} page(s). Its text is in {entry.text_path} ({a.lines:,} lines, "
                f"'--- page N ---' before each page): Read it in parts, and Grep it for specific terms.")


    if a.kind == "text":
        if entry.inline:
            return f"- {entry.path}: text, included below."
        return f"- {entry.path}: text, {a.lines:,} lines; Read it in parts."
    return f"- {entry.path}: {a.mime} file, {_size(a.size)}."


def _figure_hint(placed: list[Placed]) -> str:
    """Only a model that can see images benefits from this, so ``describe`` gates it."""
    return ("Read a PDF with page_number=N to see that page as an image (figures and diagrams are not in the text)."
            if any(p.attachment.kind == "pdf" for p in placed) else "")


def describe(placed: list[Placed], heading: str = "# Attachments", vision: bool = True) -> str:
    """The message section listing attachments, with small documents' text.

    Args:
        vision: Whether the model reading this can see images (it then gets the
            hints about looking at images and PDF pages again).
    """
    if not placed:
        return ""
    lines = [f"{heading} (saved in {ATTACHMENT_DIR}/; they are not part of the project)", *map(_line, placed)]
    if vision and any(p.data_url for p in placed):
        # Local servers stop re-processing an image once it is a few turns back; Read hands it over again.
        lines.append("Read an image file again whenever you need another look at it.")
    if vision and _figure_hint(placed):
        lines.append(_figure_hint(placed))
    for entry in placed:
        if entry.inline:
            lines.append(f'\n<attachment path="{entry.path}">\n{entry.inline.strip()}\n</attachment>')
    return "\n".join(lines)


def user_content(text: str, placed: list[Placed]) -> str | list[dict[str, Any]]:
    """Message content: plain text, or text followed by the images as ``image_url`` parts."""
    images = [p for p in placed if p.data_url]
    if not images:
        return text
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for entry in images:
        parts.append({"type": "text", "text": f"[image {entry.path}]"})
        parts.append({"type": "image_url", "image_url": {"url": entry.data_url}})
    return parts
