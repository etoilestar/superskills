import json

import pytest

from backend.services.creator import api


@pytest.mark.asyncio
async def test_existing_skills_distinguish_resumable_contracts_from_skill_md(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)

    resumable = tmp_path / "with-contracts"
    metadata = resumable / ".creator"
    metadata.mkdir(parents=True)
    (resumable / "SKILL.md").write_text("# Existing behavior", encoding="utf-8")
    (metadata / "blueprint.md").write_text("saved blueprint", encoding="utf-8")
    (metadata / "creation_plan.json").write_text(json.dumps({"plan": {"function_items": []}}), encoding="utf-8")
    (metadata / "interface_contracts.json").write_text("{}", encoding="utf-8")

    legacy = tmp_path / "skill-md-only"
    legacy.mkdir()
    (legacy / "SKILL.md").write_text("# Legacy behavior", encoding="utf-8")

    choices = await api.list_creator_existing_skills()

    assert [(item["skill_name"], item["edit_strategy"]) for item in choices] == [
        ("skill-md-only", "rebuild_from_skill_md"),
        ("with-contracts", "resume_saved_contracts"),
    ]
    assert choices[0]["blueprint_text"] == ""
    assert choices[1]["blueprint_text"] == "saved blueprint"


def test_existing_skill_context_never_treats_skill_md_as_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    skill = tmp_path / "legacy"
    skill.mkdir()
    (skill / "SKILL.md").write_text("Performs the current feature.", encoding="utf-8")

    context = api._read_prepare_existing_skill_context("legacy")

    assert context["skill_md"] == "Performs the current feature."
    assert context["has_saved_contracts"] is False
    assert context["edit_strategy"] == "rebuild_from_skill_md"


def test_empty_skill_selection_preserves_create_context():
    """The default new-Skill path must not inherit any edit strategy or files."""
    assert api._read_prepare_existing_skill_context(None) == {}
    assert api._read_prepare_existing_skill_context("") == {}
