import json

from backend.services.creator import api


def test_creator_contracts_are_persisted_and_reused_for_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    skill = tmp_path / "demo"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
    (skill / "scripts" / "main.py").write_text("print('existing')\n", encoding="utf-8")

    first = api._persist_creator_contracts(
        "demo",
        blueprint_text="# Blueprint v1",
        requirement_graph={},
        function_items=[],
        responsibility_edges=[],
        files=[],
        mode="create",
        user_request="create it",
    )

    assert first["revision"] == 1
    assert (skill / ".creator" / "blueprint.md").read_text(encoding="utf-8") == "# Blueprint v1"
    assert json.loads((skill / ".creator" / "interface_contract.json").read_text(encoding="utf-8")) == {
        "input_ports": [], "output_ports": [], "edge_mappings": []
    }

    context = api._read_prepare_existing_skill_context("demo")
    assert context["saved_contracts_available"] is True
    assert context["saved_creator_contracts"]["blueprint_text"] == "# Blueprint v1"
    assert context["script_contents"]["scripts/main.py"] == "print('existing')\n"
    assert "reconstructed_contract_baseline" not in context

    second = api._persist_creator_contracts(
        "demo", blueprint_text="# Blueprint v2", requirement_graph={}, mode="revise",
        human_feedback="add CSV output",
    )
    assert second["revision"] == 2
    assert second["change_request"]["human_feedback"] == "add CSV output"


def test_revision_context_reconstructs_baseline_for_legacy_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    skill = tmp_path / "legacy"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Legacy\n", encoding="utf-8")
    (skill / "scripts" / "run.py").write_text("print('legacy')\n", encoding="utf-8")

    context = api._read_prepare_existing_skill_context("legacy")

    assert context["saved_contracts_available"] is False
    baseline = context["reconstructed_contract_baseline"]
    assert baseline["source"] == "existing_skill_artifacts"
    assert baseline["script_contents"]["scripts/run.py"] == "print('legacy')\n"

