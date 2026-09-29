import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from scripted_server import ScriptedLLM  # noqa: E402

from localagent.models.registry import ModelSpec, Provider, Registry, RoleSpec  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep every test away from the real ~/.localagent."""
    home = tmp_path / "la-home"
    monkeypatch.setenv("LOCALAGENT_HOME", str(home))
    return home


@pytest.fixture
def llm():
    with ScriptedLLM() as server:
        yield server


@pytest.fixture
def registry(llm):
    provider = Provider(name="scripted", base_url=llm.base_url, max_concurrency=2)
    model = ModelSpec(name="test", provider="scripted", model_id="test-model", context_length=8192,
                      thinking_off={"reasoning_effort": "none"})
    return Registry(providers={"scripted": provider}, models={"test": model},
                    roles={"main": RoleSpec(model="test", thinking=True)})


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return root


def make_pdf(pages: list[str]) -> bytes:
    """A real (minimal) PDF with one line of Helvetica text per page."""
    font_id = 3 + 2 * len(pages)
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [" + " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
        + f"] /Count {len(pages)} >>",
    ]
    for i, text in enumerate(pages):
        stream = f"BT /F1 18 Tf 72 700 Td ({text}) Tj ET"
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
                       f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out
