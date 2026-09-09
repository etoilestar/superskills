import json

import pytest

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


def test_derive_mode_reads_source_but_targets_new_skill():
    request = api.PreparePlanRequest(
        mode="derive",
        source_skill_name="base-skill",
        skill_name="enhanced-skill",
        user_request="add another output",
    )

    assert api._prepare_baseline_skill_name(request) == "base-skill"
    blueprint = "# Plan\n- **Skill 名称**: base-skill\n"
    assert "enhanced-skill" in api._retarget_derived_blueprint(blueprint, request)


def test_derive_hydrates_frozen_graph_instead_of_rebuilding_it(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    source = tmp_path / "base-skill" / ".creator"
    source.mkdir(parents=True)
    saved = {
        "blueprint_text": "# Existing blueprint",
        "function_items": [{"target_file": "scripts/main.py", "purpose": "stable"}],
        "responsibility_edges": [{"from_node": "platform_input_node", "to_node": "scripts/main.py"}],
        "requirement_allocations": [{"requirement_id": "req-1", "owners": ["scripts/main.py"]}],
    }
    (source / "contracts.json").write_text(json.dumps(saved), encoding="utf-8")
    request = api.PreparePlanRequest(
        mode="derive", source_skill_name="base-skill", skill_name="enhanced-skill",
        user_request="add one optional output",
    )

    hydrated, used_saved_contracts = api._hydrate_derive_request_from_saved_contracts(request)

    assert used_saved_contracts is True
    assert hydrated.previous_blueprint_text == saved["blueprint_text"]
    assert hydrated.function_items == saved["function_items"]
    assert hydrated.responsibility_edges == saved["responsibility_edges"]
    assert hydrated.requirement_allocations == saved["requirement_allocations"]


@pytest.mark.asyncio
async def test_legacy_derive_retries_generated_skill_payload_as_fileplan_envelope(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    source = tmp_path / "csv-against"
    (source / "scripts").mkdir(parents=True)
    (source / "SKILL.md").write_text("# CSV Against\n", encoding="utf-8")
    (source / "scripts" / "compare_csv.py").write_text("print('compare')\n", encoding="utf-8")
    responses = iter([
        {
            "status": "success",
            "skill_name": "csv-against-summary",
            "file_contents": {"SKILL.md": "generated too early"},
        },
        {
            "status": "needs_clarification",
            "clarifying_questions": ["请选择总结深度。A. 简要 B. 详细"],
            "review_summary": {
                "goal": "", "input": "", "output": "",
                "workflow": [], "risks": [], "changes": [],
            },
            "internal_blueprint_text": "",
            "skill_name": "csv-against-summary",
            "blockers": [],
        },
    ])
    calls = []

    async def fake_complete(*, messages, model, phase, response_schema):
        calls.append(messages)
        assert response_schema["properties"]["status"]["enum"] == [
            "ready", "needs_clarification", "blocked",
        ]
        assert response_schema["additionalProperties"] is False
        assert "file_contents" not in response_schema["properties"]
        return next(responses)

    monkeypatch.setattr(api, "_complete_creator_json_object_once", fake_complete)
    monkeypatch.setattr(api, "route_model", lambda *_args, **_kwargs: type("Route", (), {"model": "planner"})())

    result = await api._generate_internal_blueprint_or_questions(api.PreparePlanRequest(
        mode="derive",
        source_skill_name="csv-against",
        skill_name="csv-against-summary",
        user_request="需要加上对表格内容的总结功能",
    ))

    assert len(calls) == 2
    assert "FILEPLAN ENVELOPE REPAIR" in calls[1][0]["content"]
    assert result["status"] == "needs_clarification"
    assert result["clarifying_questions"] == ["请选择总结深度。A. 简要 B. 详细"]


@pytest.mark.asyncio
async def test_legacy_derive_retries_ready_prose_as_a_full_blueprint(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    source = tmp_path / "csv-against"
    (source / "scripts").mkdir(parents=True)
    (source / "SKILL.md").write_text("# CSV Against\n", encoding="utf-8")
    responses = iter([
        {
            "status": "ready",
            "clarifying_questions": [],
            "review_summary": {
                "goal": "add summary", "input": "CSV", "output": "report",
                "workflow": [], "risks": [], "changes": ["add summary"],
            },
            "internal_blueprint_text": "The skill now includes a summary.",
            "skill_name": "csv-against-summary",
            "blockers": [],
        },
        {
            "status": "needs_clarification",
            "clarifying_questions": ["请选择总结深度。A. 简要 B. 详细"],
            "review_summary": {
                "goal": "", "input": "", "output": "",
                "workflow": [], "risks": [], "changes": [],
            },
            "internal_blueprint_text": "",
            "skill_name": "csv-against-summary",
            "blockers": [],
        },
    ])
    requests = []

    async def fake_complete(**kwargs):
        requests.append(json.loads(kwargs["messages"][1]["content"]))
        return next(responses)

    monkeypatch.setattr(api, "_complete_creator_json_object_once", fake_complete)
    monkeypatch.setattr(api, "route_model", lambda *_args, **_kwargs: type("Route", (), {"model": "planner"})())

    result = await api._generate_internal_blueprint_or_questions(api.PreparePlanRequest(
        mode="derive", source_skill_name="csv-against",
        skill_name="csv-against-summary", user_request="增加表格内容总结",
    ))

    assert len(requests) == 2
    assert requests[1]["invalid_internal_blueprint_text"] == "The skill now includes a summary."
    assert requests[1]["fileplan_protocol_errors"]
    assert "existing_skill_context" in requests[1]
    assert result["status"] == "needs_clarification"


@pytest.mark.asyncio
async def test_derived_initialization_copies_only_planned_baseline_files(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    source = tmp_path / "base"
    (source / "scripts").mkdir(parents=True)
    (source / "assets").mkdir()
    (source / ".creator").mkdir()
    (source / "SKILL.md").write_text("# Base\n", encoding="utf-8")
    (source / "scripts" / "keep.py").write_text("print('keep')\n", encoding="utf-8")
    (source / "scripts" / "obsolete.py").write_text("print('old')\n", encoding="utf-8")
    (source / "assets" / "logo.txt").write_text("logo", encoding="utf-8")
    (source / ".creator" / "contracts.json").write_text("{}", encoding="utf-8")

    def fake_run_action(_payload):
        target = tmp_path / "enhanced"
        target.mkdir(parents=True, exist_ok=True)
        return {"success": True, "path": str(target), "message": "ok"}

    monkeypatch.setattr(api, "run_action", fake_run_action)
    response = await api.init_skill(api.InitSkillRequest(
        skill_name="enhanced",
        source_skill_name="base",
        baseline_files=["SKILL.md", "scripts/keep.py", "assets/logo.txt"],
    ))

    target = tmp_path / "enhanced"
    assert response.success is True
    assert (target / "scripts" / "keep.py").read_text(encoding="utf-8") == "print('keep')\n"
    assert (target / "assets" / "logo.txt").read_text(encoding="utf-8") == "logo"
    assert not (target / "scripts" / "obsolete.py").exists()
    assert not (target / ".creator" / "contracts.json").exists()
    assert (source / "scripts" / "keep.py").read_text(encoding="utf-8") == "print('keep')\n"
    assert (source / ".creator" / "contracts.json").read_text(encoding="utf-8") == "{}"


@pytest.mark.asyncio
async def test_legacy_derive_confirmation_reconstructs_blueprint_from_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    source = tmp_path / "csv-against"
    (source / "scripts").mkdir(parents=True)
    (source / "SKILL.md").write_text("# CSV Against\n", encoding="utf-8")
    (source / "scripts" / "compare_csv.py").write_text("print('compare')\n", encoding="utf-8")

    planner_requests = []

    async def fake_generate(request):
        planner_requests.append(request)
        return {
            "status": "needs_clarification",
            "clarifying_questions": ["planner was reached"],
            "review_summary": {},
            "internal_blueprint_text": "",
            "skill_name": "csv-against-summary",
            "blockers": [],
        }

    monkeypatch.setattr(api, "_generate_internal_blueprint_or_questions", fake_generate)

    response = await api._prepare_plan_impl(api.PreparePlanRequest(
        mode="derive",
        source_skill_name="csv-against",
        skill_name="csv-against-summary",
        user_request="需要加上对表格内容的总结功能",
        human_feedback="B. 暂时没有补充，按已有信息继续",
        prepare_action="confirm",
    ))

    assert len(planner_requests) == 1
    assert response.status == "needs_clarification"
    assert response.clarifying_questions[0].startswith("planner was reached")
    assert not any(
        isinstance(blocker, dict) and blocker.get("code") == "missing_confirmed_blueprint_state"
        for blocker in response.creation_blockers
    )


@pytest.mark.asyncio
async def test_plain_creation_initialization_remains_source_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)

    def fake_run_action(_payload):
        target = tmp_path / "brand-new"
        target.mkdir(parents=True, exist_ok=True)
        return {"success": True, "path": str(target), "message": "ok"}

    monkeypatch.setattr(api, "run_action", fake_run_action)
    response = await api.init_skill(api.InitSkillRequest(skill_name="brand-new"))

    assert response.success is True
    assert (tmp_path / "brand-new").is_dir()
