"""Focused regression tests for Creator final ToolPool selection inputs."""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.creator import api


def test_system_image_generation_is_selectable_with_existing_function_contract():
    catalog = api._creator_tool_catalog_for_planner()
    by_id = {tool["tool_id"]: tool for tool in catalog}

    assert "system_image_generation" in by_id
    functions = by_id["system_image_generation"]["functions"]
    assert any(
        function.get("function_name") == "generate_stable_diffusion_image"
        and function.get("import_path")
        == "backend.services.skill_runtime"
        for function in functions
    )


def test_creator_selectable_catalog_filters_capability_shells(monkeypatch):
    callable_tool = {
        "functions": [
            {
                "function_name": "call_me",
                "import_path": "backend.services.skill_runtime",
            }
        ]
    }
    no_functions = {"functions": []}
    missing_name = {
        "functions": [
            {
                "function_name": "",
                "import_path": "backend.services.skill_runtime",
            }
        ]
    }
    missing_import_path = {
        "functions": [
            {
                "function_name": "call_me",
                "import_path": "",
            }
        ]
    }

    assert api._creator_tool_has_callable_contract(callable_tool)
    assert not api._creator_tool_has_callable_contract(no_functions)
    assert not api._creator_tool_has_callable_contract(missing_name)
    assert not api._creator_tool_has_callable_contract(missing_import_path)


def test_final_tool_selector_prompt_describes_optional_candidate_pool():
    source = inspect.getsource(api._plan_final_tool_pool)

    assert "ToolPool 是当前 Skill 允许代码生成模型优先使用的真实工具候选池 / 白名单" in source
    assert "不表示任何 scripts/*.py 必须调用该工具" in source
    assert "selected=true 只表示将真实工具加入 Skill ToolPool" in source
    assert "供代码生成模型选择；不表示脚本必须调用该工具" in source


def test_final_tool_selector_prompt_does_not_force_missing_plan_capability_false():
    source = inspect.getsource(api._plan_final_tool_pool)

    assert "当前 Plan 没有声明对应能力" not in source
    assert "Plan.required_capabilities 没有逐字声明该工具对应 capability，也可以设为 true" in source


def test_embedding_recall_keeps_exact_match_plus_top_k_union_contract():
    source = inspect.getsource(api._recall_creator_tool_candidates)

    assert "exact Registry capability matches" in source
    assert "UNION" in source
    assert "per-query embedding top-k" in source

import pytest


@pytest.mark.asyncio
async def test_final_tool_selector_runs_one_convergence_revision(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    monkeypatch.setattr(api, "_recall_creator_tool_candidates", lambda file_specs, top_k: ([{"tool_id":"callable_alpha","functions":[{"function_name":"run","import_path":"x"}]}], "test"))
    monkeypatch.setattr(api, "_apply_planner_tool_pool_patch", lambda **kwargs: {"patch_present": True})

    async def fake_json(**kwargs):
        calls.append(kwargs)
        if kwargs["phase"] == "final_tool_selection":
            return {"decisions":{"callable_alpha": False}}
        return {"decisions":{"callable_alpha": True}}

    monkeypatch.setattr(api, "_complete_creator_json_object_once", fake_json)
    result = await api._plan_final_tool_pool(skill_name="demo", file_specs=[{"path":"scripts/a.py","required":True}], responsibility_graph={"dataflow_edges":[],"items":[{}]})
    assert [call["phase"] for call in calls] == ["final_tool_selection", "final_tool_selection_convergence"]
    assert calls[1]["messages"][1]["content"].find('"draft_decisions"') >= 0
    assert result["desired_tool_ids"] == ["callable_alpha"]


@pytest.mark.asyncio
async def test_tool_selector_convergence_replaces_draft_decisions(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    monkeypatch.setattr(api, "_recall_creator_tool_candidates", lambda file_specs, top_k: ([{"tool_id":"callable_alpha","functions":[{"function_name":"run","import_path":"x"}]},{"tool_id":"callable_beta","functions":[{"function_name":"run","import_path":"x"}]}], "test"))
    monkeypatch.setattr(api, "_apply_planner_tool_pool_patch", lambda **kwargs: {"patch_present": True})

    async def fake_json(**kwargs):
        if kwargs["phase"] == "final_tool_selection":
            return {"decisions":{"callable_alpha": False, "callable_beta": True}}
        return {"decisions":{"callable_alpha": True, "callable_beta": True}}

    monkeypatch.setattr(api, "_complete_creator_json_object_once", fake_json)
    result = await api._plan_final_tool_pool(skill_name="demo", file_specs=[{"path":"scripts/a.py","required":True}])
    assert result["desired_tool_ids"] == ["callable_alpha", "callable_beta"]


@pytest.mark.asyncio
async def test_tool_convergence_cannot_expand_candidate_catalog(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    monkeypatch.setattr(api, "_recall_creator_tool_candidates", lambda file_specs, top_k: ([{"tool_id":"callable_alpha","functions":[{"function_name":"run","import_path":"x"}]}], "test"))
    monkeypatch.setattr(api, "_apply_planner_tool_pool_patch", lambda **kwargs: {"patch_present": True})

    async def fake_json(**kwargs):
        if kwargs["phase"] == "final_tool_selection":
            return {"decisions":{"callable_alpha": False}}
        return {"decisions":{"callable_alpha": True, "callable_extra": True}}

    monkeypatch.setattr(api, "_complete_creator_json_object_once", fake_json)
    result = await api._plan_final_tool_pool(skill_name="demo", file_specs=[{"path":"scripts/a.py","required":True}])
    assert result["desired_tool_ids"] == []
