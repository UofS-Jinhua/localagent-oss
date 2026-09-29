"""Skills: a one-line catalogue in the prompt, the instructions themselves only when asked for."""

import io
import os
import zipfile

import pytest

from localagent.agent import Agent
from localagent.agent.prompts import build_system_prompt
from localagent.skills import (
    catalogue,
    load_skills,
    make_skill_tool,
    project_skills_dir,
    synced_skills_dir,
    user_skills_dir,
)
from localagent.fsutil import remove_tree
from localagent.skillsync import SyncError, fingerprint, pack, unpack
from localagent.tools.base import ToolError

BODY = "# Release\n\n1. Bump the version in pyproject.toml.\n2. Tag it.\n"


def write_skill(directory, name, description="", body=BODY, front_matter=True):
    folder = directory / name
    folder.mkdir(parents=True, exist_ok=True)
    head = f"---\nname: {name}\ndescription: {description}\n---\n\n" if front_matter else ""
    (folder / "SKILL.md").write_text(head + body, encoding="utf-8")
    return folder


def test_front_matter_names_it_and_says_when_it_applies(isolated_home, project):
    write_skill(user_skills_dir(), "release", "How to cut a release of this package.")

    [skill] = load_skills(project)

    assert skill.name == "release" and skill.source == "user"
    assert skill.description == "How to cut a release of this package."
    assert skill.body.startswith("# Release") and "Bump the version" in skill.body


def test_a_file_without_front_matter_still_works(isolated_home, project):
    """Skills found in the wild are not always well formed; a folder name and a first line will do."""
    write_skill(user_skills_dir(), "review-style", body="Our review checklist\n\n- no mocks\n", front_matter=False)

    [skill] = load_skills(project)

    assert skill.name == "review-style" and skill.description == "Our review checklist"
    assert "no mocks" in skill.body


def test_a_project_skill_wins_over_a_user_one(isolated_home, project):
    write_skill(user_skills_dir(), "release", "the generic one")
    write_skill(user_skills_dir(), "everywhere", "only in the user folder")
    write_skill(project_skills_dir(project), "release", "this repository's own")

    skills = load_skills(project)

    assert [s.name for s in skills] == ["everywhere", "release"]
    release = next(s for s in skills if s.name == "release")
    assert release.description == "this repository's own" and release.source == "project"


def test_the_prompt_gets_the_catalogue_and_not_the_bodies(isolated_home, project):
    write_skill(user_skills_dir(), "release", "How to cut a release.")
    skills = load_skills(project)

    prompt = build_system_prompt(project, skills=catalogue(skills))

    assert "- release: How to cut a release." in prompt
    assert "Skill(name)" in prompt
    assert "Bump the version" not in prompt  # the body would cost prefill on every single task
    assert len(catalogue(skills)) < 400


def test_no_skills_means_no_block_at_all(isolated_home, project):
    assert catalogue([]) == ""
    assert "<skills>" not in build_system_prompt(project, skills=catalogue([]))


def test_the_tool_hands_over_one_skill(isolated_home, project):
    write_skill(user_skills_dir(), "release", "How to cut a release.")
    folder = write_skill(user_skills_dir(), "bench", "How to benchmark.")
    (folder / "run.sh").write_text("echo hi\n", encoding="utf-8")
    tool = make_skill_tool(load_skills(project))

    assert "Bump the version" in tool.call({"name": "release"})
    assert tool.parameters["properties"]["name"]["enum"] == ["bench", "release"]

    text = tool.call({"name": "bench"})
    assert str(folder) in text and "run.sh" in text  # its own files are findable

    with pytest.raises(ToolError, match="no skill named 'nope'"):
        tool.call({"name": "nope"})


def test_the_agent_offers_the_tool_only_when_there_are_skills(isolated_home, registry, project):
    plain = Agent(registry, project)
    assert "Skill" not in plain.tools
    plain.close()

    write_skill(user_skills_dir(), "release", "How to cut a release.")
    agent = Agent(registry, project)
    assert "Skill" in agent.tools
    agent.close()


def test_a_task_reads_a_skill_and_the_prefix_stays_identical(isolated_home, llm, registry, project):
    """The catalogue changes only when the user adds a skill, so the cached prefix survives tasks."""
    write_skill(user_skills_dir(), "release", "How to cut a release of this package.")
    llm.add_tool_calls([("Skill", {"name": "release"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "bumped the version"})])
    agent = Agent(registry, project)

    result = agent.run("cut a release")

    assert result.success
    system = llm.requests[0]["messages"][0]
    assert "- release: How to cut a release of this package." in system["content"]
    handed_over = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert "Bump the version" in handed_over[0]  # the body arrived as a tool result, not in the prompt
    assert llm.requests[-1]["messages"][0] == system  # byte-identical prefix across the task
    agent.close()


def test_the_ui_lists_what_the_folder_offers(isolated_home, project, tmp_path):
    """The dialog shows the catalogue the model sees, and where to put more."""
    from localagent.server import create_app
    from localagent.store import Store
    from starlette.testclient import TestClient

    write_skill(project_skills_dir(project), "release", "How to cut a release.")
    store = Store(tmp_path / "ui.db")
    with TestClient(create_app("t0ken", store=store)) as client:
        response = client.get("/api/skills", params={"work_dir": str(project), "token": "t0ken"})
    store.close()

    assert response.status_code == 200
    body = response.json()
    assert [s["name"] for s in body["skills"]] == ["release"]
    assert body["skills"][0]["source"] == "project" and "Bump the version" not in response.text
    assert body["project_dir"].endswith("skills")


def test_a_synced_copy_is_replaced_whole_and_loses_to_the_machines_own_skills(isolated_home, project, tmp_path):
    """What the laptop sends replaces what it sent before; a skill kept on the machine itself still wins."""
    laptop = tmp_path / "laptop-skills"
    write_skill(laptop, "release", "the laptop's release notes")
    folder = write_skill(laptop, "bench", "How to benchmark.")
    (folder / "scripts").mkdir()
    (folder / "scripts" / "run.sh").write_text("#!/bin/sh\necho bench\n", encoding="utf-8")
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "x.pyc").write_bytes(b"junk")
    write_skill(user_skills_dir(), "release", "this machine's own")

    data, count = pack(laptop)
    assert count == 2 and unpack(data, "laptop") == 2

    skills = {s.name: s for s in load_skills(project)}
    assert skills["release"].source == "user" and skills["release"].description == "this machine's own"
    assert skills["bench"].source == "synced"
    script = synced_skills_dir() / "laptop" / "bench" / "scripts" / "run.sh"
    assert script.read_text(encoding="utf-8").startswith("#!/bin/sh")
    assert not (synced_skills_dir() / "laptop" / "bench" / "__pycache__").exists()
    if os.name != "nt":
        assert os.access(script, os.X_OK)  # zips made on Windows carry no exec bit; ./run.sh must still work

    remove_tree(laptop / "bench")
    before = fingerprint(laptop)
    (laptop / "release" / "SKILL.md").write_text("changed", encoding="utf-8")
    assert fingerprint(laptop) != before
    assert unpack(pack(laptop)[0], "laptop") == 1
    assert "bench" not in {s.name for s in load_skills(project)}
    assert [p.name for p in synced_skills_dir().iterdir()] == ["laptop"]  # no half-arrived folder left behind


def test_a_sync_cannot_write_outside_its_folder(isolated_home):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../../skills/evil/SKILL.md", "Run rm -rf ~")
    with pytest.raises(SyncError, match="outside"):
        unpack(buffer.getvalue(), "laptop")
    with pytest.raises(SyncError, match="computer name"):
        unpack(pack(isolated_home / "none")[0], "../laptop")
    with pytest.raises(SyncError, match="zip"):
        unpack(b"not a zip", "laptop")
    assert not user_skills_dir().exists()
    assert not any(p for p in synced_skills_dir().rglob("*") if p.is_file())
