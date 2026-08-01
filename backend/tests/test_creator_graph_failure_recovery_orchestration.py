import json
from collections import Counter
from unittest.mock import AsyncMock

import pytest

from backend.services.creator import api


def _request():
    return api.PreparePlanRequest(user_request="Build a generic processor", human_feedback="")


def _script_block(path, *, inputs, outputs=("result",), role="generic_script", capabilities=()):
    return (
        f"- path: `{path}`\n"
        f"  role: {role}\n"
        f"  inputs: [{', '.join(inputs)}]\n"
        f"  outputs: [{', '.join(outputs)}]\n"
        "  dependencies: []\n"
        f"  required_capabilities: [{', '.join(capabilities)}]\n"
        "  forbidden_capabilities: []\n"
        "  references: []\n"
        "  constraints: []"
    )


def _blueprint(*blocks):
    files = "\n".join([
        "- path: `SKILL.md`\n  role: skill_overview\n  inputs: []\n  outputs: []\n"
        "  dependencies: []\n  required_capabilities: []\n  forbidden_capabilities: []\n"
        "  references: []\n  constraints: []",
        *blocks,
    ])
    return f"""## 📋 Skill 架构蓝图
### 基本信息
- **Skill Name**: generic-processor
### I/O Contract
- **Input**: runtime value
- **Output**: structured result
### 目录结构
- SKILL.md
- scripts/
### Workflow Logic
1. Process the declared input.
### SkillPlan / 文件职责计划
{files}
### 宿主执行方式
- **需要脚本/命令**: execute declared scripts
- direct answer: return result
### Resource List
- none
"""


def _ready(blueprint):
    return {
        "status": "ready", "clarifying_questions": [], "review_summary": {},
        "internal_blueprint_text": blueprint, "skill_name": "generic-processor", "blockers": [],
    }


def _item(path, *, inputs, outputs=("result",)):
    return {
        "target_file": path, "role": "generic_script", "purpose": f"run {path}",
        "inputs": list(inputs), "outputs": list(outputs),
        "required_capabilities": [], "constraints": [],
    }


def _output_edge(path, output="result"):
    return {
        "from_node": path, "from_output": output,
        "to_node": "platform_output_node", "to_input": "file_outputs",
        "purpose": "deliver result", "constraints": [],
    }


def _input_edge(path, target_input):
    return {
        "from_node": "platform_input_node", "from_output": "user_request",
        "to_node": path, "to_input": target_input,
        "purpose": "provide input", "constraints": [],
    }


async def _run_recovery(
    monkeypatch, *, initial_items, initial_edges, repair_edges, regenerated_edges,
    replanned_blueprint=None, rebuilt_items=None, rebuilt_edges=None,
    rebuilt_review=None, observe=None, calls=None,
):
    monkeypatch.setattr(api.settings, "creator_graph_binding_mode", "legacy")
    initial_blueprint = _blueprint(*[
        _script_block(item["target_file"], inputs=item["inputs"], outputs=item["outputs"])
        for item in initial_items
    ])
    if calls is None:
        calls = Counter()

    async def planner_once(*_args, **_kwargs):
        return json.dumps(_ready(initial_blueprint))

    async def bind(**_kwargs):
        calls["bind"] += 1
        if calls["bind"] == 1:
            return {"function_items": initial_items, "responsibility_edges": initial_edges}
        return {"function_items": rebuilt_items, "responsibility_edges": rebuilt_edges}

    async def converge(**_kwargs):
        return {**_ready(initial_blueprint), "function_items": initial_items, "responsibility_edges": initial_edges}

    async def repair(**kwargs):
        calls["repair"] += 1
        if observe:
            observe("after_repair", calls.copy())
        return {"function_items": kwargs["function_items"], "responsibility_edges": repair_edges}

    async def regenerate(**kwargs):
        calls["regenerate"] += 1
        if observe:
            observe("after_regenerate", calls.copy())
        return {"function_items": kwargs["function_items"], "responsibility_edges": regenerated_edges}

    async def replan(**_kwargs):
        calls["replan"] += 1
        return replanned_blueprint

    async def review(**_kwargs):
        calls["review"] += 1
        return rebuilt_review or {"passed": True, "issues": []}

    real_resolve_targets = api._resolve_allowed_function_item_targets_from_blueprint

    def resolve_targets(blueprint_text):
        calls["resolve_targets"] += 1
        return real_resolve_targets(blueprint_text)

    monkeypatch.setattr(api, "complete_creator_role_once", planner_once)
    monkeypatch.setattr(api, "_plan_executable_requirement_allocations", AsyncMock(return_value={
        "requirement_allocations": [], "requirement_channels": {},
    }))
    monkeypatch.setattr(api, "_review_blueprint_semantic_closure", AsyncMock(return_value={
        "passed": True, "issues": [],
    }))
    monkeypatch.setattr(api, "_bind_executable_responsibility_plan", bind)
    monkeypatch.setattr(api, "_converge_ready_executable_plan", converge)
    monkeypatch.setattr(api, "_repair_responsibility_graph_alignment", repair)
    monkeypatch.setattr(api, "_regenerate_responsibility_graph", regenerate)
    monkeypatch.setattr(api, "_replan_blueprint_for_graph_closure", replan)
    monkeypatch.setattr(api, "_review_responsibility_graph_alignment", review)
    monkeypatch.setattr(api, "_resolve_allowed_function_item_targets_from_blueprint", resolve_targets)
    monkeypatch.setattr(
        api, "_normalize_prepare_clarifying_questions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("internal recovery asked for clarification")),
    )
    result = await api._generate_internal_blueprint_or_questions(_request())
    return result, calls


@pytest.mark.asyncio
async def test_same_unresolved_input_runs_repair_regeneration_and_one_replan_then_rebuild(monkeypatch):
    items = [
        _item("scripts/a.py", inputs=(), outputs=("result",)),
        _item("scripts/b.py", inputs=("required_value",), outputs=("final_result",)),
    ]
    unresolved = [_output_edge("scripts/b.py", "final_result")]
    replanned = _blueprint(
        _script_block("scripts/a.py", inputs=(), outputs=("result",)),
        _script_block("scripts/b.py", inputs=("source_value",), outputs=("final_result",)),
    )
    rebuilt_items = [items[0], _item("scripts/b.py", inputs=("source_value",), outputs=("final_result",))]
    rebuilt_edges = [_input_edge("scripts/b.py", "source_value"), _output_edge("scripts/b.py", "final_result")]
    observations = []

    result, calls = await _run_recovery(
        monkeypatch, initial_items=items, initial_edges=unresolved,
        repair_edges=unresolved, regenerated_edges=unresolved,
        replanned_blueprint=replanned, rebuilt_items=rebuilt_items, rebuilt_edges=rebuilt_edges,
        observe=lambda phase, snapshot: observations.append((phase, snapshot["replan"])),
    )

    assert observations == [("after_repair", 0), ("after_regenerate", 0)]
    assert calls["repair"] == calls["regenerate"] == calls["replan"] == 1
    assert calls["bind"] == 2
    assert calls["resolve_targets"] == 2  # initial freeze and post-replan re-freeze
    assert calls["review"] == 1
    assert result["function_items"] == api.normalize_structured_function_items(
        rebuilt_items, source="test"
    )
    assert result["responsibility_edges"] == rebuilt_edges
    assert result["status"] == "ready" and result.get("clarifying_questions") == []
    assert result["metrics"] == {
        "legacy_graph_status": "committed",
        "legacy_repair_count": 1,
        "legacy_regeneration_count": 1,
        "legacy_blueprint_replan_count": 1,
        "legacy_edge_count": len(rebuilt_edges),
    }


@pytest.mark.asyncio
async def test_replanned_graph_still_unresolved_cleanly_stops(monkeypatch):
    items = [_item("scripts/b.py", inputs=("required_value",), outputs=("final_result",))]
    unresolved = [_output_edge("scripts/b.py", "final_result")]
    calls = Counter()
    with pytest.raises(api.PreparePlanProtocolError, match="still lacks required input provenance"):
        await _run_recovery(
            monkeypatch, initial_items=items, initial_edges=unresolved,
            repair_edges=unresolved, regenerated_edges=unresolved,
            replanned_blueprint=_blueprint(_script_block("scripts/b.py", inputs=("required_value",), outputs=("final_result",))),
            rebuilt_items=items, rebuilt_edges=unresolved,
            calls=calls,
        )
    assert calls["repair"] == calls["regenerate"] == calls["replan"] == 1
    assert calls["bind"] == 2 and calls["review"] == 0


@pytest.mark.asyncio
async def test_replanned_graph_semantic_failure_cleanly_stops(monkeypatch):
    items = [_item("scripts/b.py", inputs=("invalid_required_value",), outputs=("final_result",))]
    unresolved = [_output_edge("scripts/b.py", "final_result")]
    rebuilt_items = [_item("scripts/b.py", inputs=("source_value",), outputs=("final_result",))]
    calls = Counter()
    with pytest.raises(api.PreparePlanProtocolError, match="failed full validation"):
        await _run_recovery(
            monkeypatch, initial_items=items, initial_edges=unresolved,
            repair_edges=unresolved, regenerated_edges=unresolved,
            replanned_blueprint=_blueprint(_script_block("scripts/b.py", inputs=("source_value",), outputs=("final_result",))),
            rebuilt_items=rebuilt_items,
            rebuilt_edges=[_input_edge("scripts/b.py", "source_value"), _output_edge("scripts/b.py", "final_result")],
            rebuilt_review={"passed": False, "issues": [{"id": "alignment"}]},
            calls=calls,
        )
    assert calls["repair"] == calls["regenerate"] == calls["replan"] == 1
    assert calls["review"] == 1


@pytest.mark.asyncio
async def test_graph_local_endpoint_failure_never_replans_blueprint(monkeypatch):
    item = _item("scripts/a.py", inputs=(), outputs=("result",))
    invalid = [{**_output_edge("scripts/a.py"), "to_node": "unknown_node"}]
    calls = Counter()
    with pytest.raises(api.PreparePlanProtocolError):
        await _run_recovery(
            monkeypatch, initial_items=[item], initial_edges=invalid,
            repair_edges=invalid, regenerated_edges=invalid,
            calls=calls,
        )
    assert calls["repair"] == calls["regenerate"] == 1
    assert calls["replan"] == 0


@pytest.mark.asyncio
async def test_changed_unresolved_fingerprint_is_progress_and_does_not_replan(monkeypatch):
    item = _item("scripts/b.py", inputs=("x", "y"), outputs=("final_result",))
    initial = [_input_edge("scripts/b.py", "y"), _output_edge("scripts/b.py", "final_result")]
    later = [_input_edge("scripts/b.py", "x"), _output_edge("scripts/b.py", "final_result")]
    calls = Counter()
    with pytest.raises(api.PreparePlanProtocolError):
        await _run_recovery(
            monkeypatch, initial_items=[item], initial_edges=initial,
            repair_edges=later, regenerated_edges=later,
            calls=calls,
        )
    assert calls["repair"] == calls["regenerate"] == 1
    assert calls["replan"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope_violation", ["unrelated", "forbidden_target", "add_file", "remove_file", "rename_file"]
)
async def test_blueprint_replan_scope_guard_rejects_out_of_scope_changes(monkeypatch, scope_violation):
    original = _blueprint(
        _script_block("scripts/a.py", inputs=(), outputs=("result",)),
        _script_block("scripts/b.py", inputs=("required_value",), outputs=("final_result",)),
    )
    if scope_violation == "unrelated":
        replacement = _blueprint(
            _script_block("scripts/a.py", inputs=(), outputs=("changed_result",)),
            _script_block("scripts/b.py", inputs=(), outputs=("final_result",)),
        )
        expected = "modified unrelated FilePlan entry"
    else:
        if scope_violation == "forbidden_target":
            replacement = _blueprint(
                _script_block("scripts/a.py", inputs=(), outputs=("result",)),
                _script_block("scripts/b.py", inputs=(), outputs=("changed_final_result",)),
            )
            expected = "exceeded the affected contract scope"
        elif scope_violation == "add_file":
            replacement = _blueprint(
                _script_block("scripts/a.py", inputs=(), outputs=("result",)),
                _script_block("scripts/b.py", inputs=(), outputs=("final_result",)),
                _script_block("scripts/c.py", inputs=(), outputs=("extra_result",)),
            )
            expected = "changed the frozen FilePlan path domain"
        elif scope_violation == "remove_file":
            replacement = _blueprint(
                _script_block("scripts/b.py", inputs=(), outputs=("final_result",)),
            )
            expected = "changed the frozen FilePlan path domain"
        else:
            replacement = _blueprint(
                _script_block("scripts/a.py", inputs=(), outputs=("result",)),
                _script_block("scripts/renamed.py", inputs=(), outputs=("final_result",)),
            )
            expected = "changed the frozen FilePlan path domain"

    planner = AsyncMock(return_value=json.dumps({"internal_blueprint_text": replacement}))
    monkeypatch.setattr(api, "complete_creator_role_once", planner)
    with pytest.raises(api.PreparePlanProtocolError, match=expected):
        await api._replan_blueprint_for_graph_closure(
            request=_request(), frozen_blueprint_text=original,
            blocking_issue={
                "category": "unresolved_input_provenance",
                "target_file": "scripts/b.py", "target_input": "required_value",
            },
            graph_construction_context={"function_items": [], "input_source_domains": []},
            planner_model="planner",
        )
