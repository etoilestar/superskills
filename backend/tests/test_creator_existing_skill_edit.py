import asyncio
import inspect
import json

from backend.services.creator import api


def _write_skill(root, *, complete=True):
    skill = root / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Demo\n已有能力", encoding="utf-8")
    if complete:
        creator = skill / ".creator"
        creator.mkdir()
        (creator / "blueprint.md").write_text("# Blueprint", encoding="utf-8")
        for name in ("requirement_graph.json", "creation_plan.json", "interface_contracts.json"):
            (creator / name).write_text("{}", encoding="utf-8")
    return skill


def test_existing_skill_design_requires_all_four_contract_files(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    skill = _write_skill(tmp_path)
    assert api._load_existing_skill_design("demo")["contract_complete"] is True
    (skill / ".creator" / "interface_contracts.json").unlink()
    loaded = api._load_existing_skill_design("demo")
    assert loaded["contract_complete"] is False
    assert loaded["skill_md"].startswith("# Demo")


def test_edit_preprocessor_crosses_into_clean_create_request(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    _write_skill(tmp_path)

    async def fake_call(messages, role, **kwargs):
        assert "existing_design" in messages[1]["content"]
        return json.dumps({"change_analysis": {}, "complete_requirement": "完整的新需求"})

    monkeypatch.setattr(api, "complete_creator_role_once", fake_call)
    request = api.PreparePlanRequest(mode="revise", skill_name="demo", user_request="增加导出")
    result = asyncio.run(api._preprocess_existing_skill_request(request))
    assert result.mode == "create"
    assert result.user_request == "完整的新需求"
    assert result.previous_blueprint_text == ""
    assert result.function_items is None
    assert result.interface_contracts is None


def test_contractless_skill_becomes_standalone_requirement_without_history(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    skill = _write_skill(tmp_path, complete=False)
    (skill / "SKILL.md").write_text("该工具可以比较两个CSV并输出报告", encoding="utf-8")
    calls = []

    async def fake_call(messages, role, **kwargs):
        calls.append((messages, kwargs))
        if kwargs["stage"] == "existing_skill_preprocess":
            payload = json.loads(messages[1]["content"])
            assert payload == {
                "skill_md": "该工具可以比较两个CSV并输出报告",
                "new_requirement": "增加差异总结",
            }
            return json.dumps({
                "skill_summary": {
                    "goal": "比较 CSV",
                    "capabilities": ["比较并报告"],
                    "inputs": ["两个 CSV"],
                    "outputs": ["比较报告"],
                },
                "complete_requirement": "基于原实现增加差异总结并保留旧功能",
            })
        assert kwargs["stage"] == "contractless_requirement_cleanup"
        return json.dumps({
            "complete_requirement": "实现一个 CSV 比较工具：接收两个 CSV 文件，比较数据，输出比较报告和差异总结。"
        })

    monkeypatch.setattr(api, "complete_creator_role_once", fake_call)
    request = api.PreparePlanRequest(
        mode="revise",
        skill_name="demo",
        user_request="增加差异总结",
        conversation_history=[{"role": "user", "content": "不要这样"}],
        human_feedback="继续",
        previous_blueprint_text="旧蓝图",
    )
    result = asyncio.run(api._preprocess_existing_skill_request(request))

    expected = api.PreparePlanRequest(
        mode="create",
        skill_name="demo",
        user_request="实现一个 CSV 比较工具：接收两个 CSV 文件，比较数据，输出比较报告和差异总结。",
    )
    assert result.user_request == expected.user_request
    assert result.mode == expected.mode
    assert result.conversation_history == expected.conversation_history == []
    assert result.human_feedback == expected.human_feedback == ""
    assert result.previous_blueprint_text == expected.previous_blueprint_text == ""
    assert len(calls) == 2
    for forbidden in ("修改已有Skill", "保留旧功能", "基于原实现"):
        assert forbidden not in result.user_request


def test_contractless_clean_requirement_does_not_need_extra_model_call(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    _write_skill(tmp_path, complete=False)

    async def fake_call(messages, role, **kwargs):
        assert kwargs["stage"] == "existing_skill_preprocess"
        return json.dumps({"complete_requirement": "实现 CSV 比较、报告生成和差异总结能力。"})

    monkeypatch.setattr(api, "complete_creator_role_once", fake_call)
    request = api.PreparePlanRequest(mode="revise", skill_name="demo", user_request="增加差异总结")
    result = asyncio.run(api._preprocess_existing_skill_request(request))
    assert result.user_request == "实现 CSV 比较、报告生成和差异总结能力。"


def test_blueprint_planner_rejects_unprocessed_edit_request():
    request = api.PreparePlanRequest(mode="revise", skill_name="demo", user_request="增加导出")
    try:
        asyncio.run(api._generate_internal_blueprint_or_questions(request))
    except api.PreparePlanProtocolError as exc:
        assert str(exc) == "Blueprint Planner requires mode=create"
    else:
        raise AssertionError("edit request reached the create-only Blueprint Planner")


def test_blueprint_planner_contains_no_edit_context_branch():
    source = inspect.getsource(api._generate_internal_blueprint_or_questions)
    assert "existing_skill_context" not in source
    assert "revise" not in source.lower()
    assert "edit" not in source.lower()


def test_frozen_interface_contract_is_carried_verbatim_without_graph_projection():
    frozen = {
        "contract_version": "planner-v1",
        "interfaces": [{"contract_id": "opaque", "runtime": {"argv": ["source"]}}],
    }
    carried = api._carry_frozen_interface_contracts(frozen)
    assert carried == frozen
    assert carried is not frozen
    assert carried["interfaces"] is not frozen["interfaces"]


def test_snapshot_persists_the_four_edit_contracts(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    api._persist_creator_design_snapshot(
        skill_name="demo",
        blueprint_text="# Final Blueprint",
        requirement_graph={"function_items": [], "responsibility_edges": []},
        creation_plan={"status": "ready"},
        interface_contracts={"interfaces": [{"contract_id": "frozen-contract-1"}]},
    )
    creator = tmp_path / "demo" / ".creator"
    assert {path.name for path in creator.iterdir()} == set(api._CREATOR_DESIGN_FILES)
    assert json.loads((creator / "creation_plan.json").read_text())["status"] == "ready"
    assert json.loads((creator / "interface_contracts.json").read_text()) == {
        "interfaces": [{"contract_id": "frozen-contract-1"}]
    }
