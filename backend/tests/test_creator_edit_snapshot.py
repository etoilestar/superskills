import json

from backend.services.creator import api


def _response():
    return api.PreparePlanResponse(
        status="ready",
        prepare_stage="ready",
        skill_name="demo",
        blueprint_text="# Blueprint",
        function_items=[{
            "target_file": "scripts/run.py",
            "inputs": [{"name": "query", "contract": {"type": "string"}}],
            "outputs": [{"name": "answer", "contract": {"type": "string"}}],
            "runtime_contract": {"entrypoint": "run"},
        }],
        responsibility_edges=[{"from_node": "__platform_input__", "to_node": "scripts/run.py"}],
        final_outputs=[{"name": "answer"}],
        requirement_graph={"function_items": [{"target_file": "scripts/run.py"}]},
    )


def test_creator_edit_snapshot_persists_blueprint_plan_and_contracts(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)

    api._persist_creator_edit_snapshot(
        skill_name="demo", blueprint_text="# Blueprint", response=_response(),
    )

    creator_dir = tmp_path / "demo" / ".creator"
    assert (creator_dir / "blueprint.md").read_text(encoding="utf-8") == "# Blueprint\n"
    snapshot = json.loads((creator_dir / "creation_plan.json").read_text(encoding="utf-8"))
    contracts = json.loads((creator_dir / "interface_contracts.json").read_text(encoding="utf-8"))
    assert snapshot["lifecycle"] == "planned"
    assert snapshot["plan"]["requirement_graph"]["function_items"][0]["target_file"] == "scripts/run.py"
    assert contracts["interfaces"][0]["runtime_contract"] == {"entrypoint": "run"}
    assert contracts["responsibility_edges"][0]["to_node"] == "scripts/run.py"


def test_completed_snapshot_remains_available_to_revision_context(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    api._persist_creator_edit_snapshot(
        skill_name="demo", blueprint_text="# Blueprint", response=_response(),
    )

    api._mark_creator_snapshot_completed("demo")

    snapshot = json.loads((tmp_path / "demo" / ".creator" / "creation_plan.json").read_text(encoding="utf-8"))
    context = api._read_prepare_existing_skill_context("demo")
    assert snapshot["lifecycle"] == "completed"
    assert snapshot["completed_at"]
    assert "# Blueprint" in context["blueprint"]
    assert "scripts/run.py" in context["creation_plan"]
    assert "entrypoint" in context["interface_contracts"]
