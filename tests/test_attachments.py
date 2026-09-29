import subprocess

import pytest
from conftest import make_pdf

from localagent.attachments import (
    ATTACHMENT_DIR,
    AttachmentStore,
    describe,
    kind_of,
    place,
    safe_name,
    unplace,
    user_content,
)
from localagent.models.probe import solid_png
from localagent.tools import Workspace, make_tools
from localagent.tools.base import ToolError, ToolOutput

PNG = solid_png(4, 4, (0, 0, 255))


def test_unplace_only_touches_copies_in_the_attachment_folder(tmp_path):
    store = AttachmentStore(tmp_path / "store")
    [placed] = place(store, [store.add(b"notes\n", "notes.md")], tmp_path / "project")
    (tmp_path / "project" / "secret.txt").write_text("the user's own file", encoding="utf-8")
    climbing = {f"{ATTACHMENT_DIR}/../secret.txt", "../secret.txt", "secret.txt", f"{ATTACHMENT_DIR}/.gitignore"}
    assert unplace(tmp_path / "project", climbing) == 0
    assert (tmp_path / "project" / "secret.txt").exists() and (tmp_path / "project" / ATTACHMENT_DIR).is_dir()
    assert unplace(tmp_path / "project", {placed.path}) == 1
    assert not (tmp_path / "project" / ATTACHMENT_DIR).exists()  # nothing but its .gitignore was left
    assert unplace(tmp_path / "project", {placed.path}) == 0  # already gone


@pytest.fixture
def store(tmp_path):
    return AttachmentStore(tmp_path / "attachments")


def tools_for(root):
    return {t.name: t for t in make_tools(Workspace(root))}


def test_kinds_and_names():
    assert kind_of("shot.PNG", PNG) == ("image", "image/png")
    assert kind_of("paper.bin", make_pdf(["x"]))[0] == "pdf"
    assert kind_of("notes.md", "中文 notes".encode()) == ("text", "text/markdown")
    assert kind_of("data.bin", b"\x00\x01\x02")[0] == "file"
    assert safe_name("../../etc/pass:wd.txt") == "pass_wd.txt"
    assert safe_name("C:\\Users\\me\\shot.png") == "shot.png"
    assert safe_name("   ") == "attachment"


def test_store_is_content_addressed_and_extracts_pdf_text(store):
    image = store.add(PNG, "shot.png")
    assert store.add(PNG, "shot.png").id == image.id
    assert store.add(PNG, "other.png").id != image.id
    assert store.get(image.id) == image and store.content_path(image).read_bytes() == PNG
    paper = store.add(make_pdf(["Attention is all you need", "Results table"]), "paper.pdf")
    assert paper.kind == "pdf" and paper.pages == 2
    text = store.text(paper)
    assert "--- page 1 ---" in text and "Attention is all you need" in text and "--- page 2 ---" in text
    with pytest.raises(KeyError):
        store.get("../../secrets")
    with pytest.raises(ValueError):
        store.add(b"", "empty.txt")


def test_place_copies_into_the_workspace_and_describes(store, project):
    image = store.add(PNG, "shot.png")
    notes = store.add(b"# Spec\nThe button must be blue.\n", "spec.md")
    long_text = store.add(("line\n" * 5000).encode(), "big.log")
    paper = store.add(make_pdf(["Hello paper"]), "paper.pdf")
    placed = place(store, [image, notes, long_text, paper], project)
    folder = project / ATTACHMENT_DIR
    assert (folder / "shot.png").read_bytes() == PNG
    assert (folder / ".gitignore").read_text(encoding="utf-8").strip().endswith("*")
    assert "Hello paper" in (folder / "paper.pdf.txt").read_text(encoding="utf-8")
    by_name = {p.attachment.name: p for p in placed}
    assert by_name["shot.png"].data_url.startswith("data:image/png;base64,")
    assert "button must be blue" in by_name["spec.md"].inline
    assert by_name["big.log"].inline == "" and by_name["paper.pdf"].text_path == f"{ATTACHMENT_DIR}/paper.pdf.txt"

    section = describe(placed)
    assert f"{ATTACHMENT_DIR}/shot.png: image, shown after this message" in section
    assert f'<attachment path="{ATTACHMENT_DIR}/spec.md">' in section and "big.log: text, 5,001 lines" in section
    content = user_content("task text", placed)
    assert content[0] == {"type": "text", "text": "task text"}
    assert content[-1]["type"] == "image_url" and content[-2]["text"] == f"[image {ATTACHMENT_DIR}/shot.png]"
    assert user_content("only text", [by_name["spec.md"]]) == "only text"


def test_place_keeps_different_files_with_the_same_name_apart(store, project):
    first, second = store.add(b"one\n", "notes.txt"), store.add(b"two\n", "notes.txt")
    assert first.id != second.id  # same name, different content
    placed = place(store, [first, second], project)
    assert placed[0].path == f"{ATTACHMENT_DIR}/notes.txt"
    assert placed[1].path != placed[0].path and (project / placed[1].path).read_text() == "two\n"
    again = place(store, [first], project, inline=set())
    assert again[0].path == placed[0].path and again[0].inline == ""


def test_read_shows_images_and_pdf_text(store, project):
    (project / "ui.png").write_bytes(PNG)
    (project / "paper.pdf").write_bytes(make_pdf(["First page text", "Second page text"]))
    tools = tools_for(project)
    output = tools["Read"].call({"file_path": "ui.png"})
    assert isinstance(output, ToolOutput) and output.images[0].startswith("data:image/png;base64,")
    text = tools["Read"].call({"file_path": "paper.pdf"})
    assert "text extracted from a 2-page PDF" in text and "Second page text" in text
    with pytest.raises(ToolError, match="not a text file"):
        tools["Edit"].call({"file_path": "paper.pdf", "old_string": "a", "new_string": "b"})


def test_grep_searches_attachments_only_when_asked(store, project):
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / "code.py").write_text("needle = 1\n", encoding="utf-8")
    place(store, [store.add(make_pdf(["the needle in the paper"]), "paper.pdf")], project)
    grep = tools_for(project)["Grep"]
    everywhere = grep.call({"pattern": "needle"})
    assert "code.py" in everywhere and ATTACHMENT_DIR not in everywhere
    inside = grep.call({"pattern": "needle", "path": ATTACHMENT_DIR})
    assert "paper.pdf.txt:" in inside
    status = subprocess.run(["git", "status", "--porcelain"], cwd=project, capture_output=True, text=True).stdout
    assert ATTACHMENT_DIR not in status


def test_a_pdf_page_can_be_read_as_a_picture(project):
    """Figures are not in the extracted text, so a vision model can ask for the page itself."""
    from conftest import make_pdf

    (project / "paper.pdf").write_bytes(make_pdf(["page one", "page two"]))
    read = tools_for(project)["Read"]

    rendered = read.call({"file_path": "paper.pdf", "page_number": 2})
    assert isinstance(rendered, ToolOutput) and "page 2 of 2" in rendered.text
    assert rendered.images[0].startswith("data:image/png;base64,")

    text = read.call({"file_path": "paper.pdf"})  # without the page: still the text
    assert isinstance(text, str) and "page one" in text
    with pytest.raises(ToolError, match="does not exist"):
        read.call({"file_path": "paper.pdf", "page_number": 9})


def test_the_attachment_note_mentions_pages_only_to_models_that_can_see(store, project):
    from conftest import make_pdf

    placed = place(store, [store.add(make_pdf(["x" * 200] * 40), "paper.pdf")], project)
    seeing = describe(placed, vision=True)
    blind = describe(placed, vision=False)
    assert "page_number=N" in seeing and "page_number=N" not in blind
