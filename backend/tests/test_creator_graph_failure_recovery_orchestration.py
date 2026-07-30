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
async def test_contract_gap_never_calls_whole_graph_or_blueprint_regeneration(monkeypatch):
    from backend.services.creator.responsibility_graph import GraphDraft, compile_responsibility_graph

    draft = GraphDraft.freeze([_item("scripts/b.py", inputs=("required_value",), outputs=("text",))])
    await compile_responsibility_graph(draft)
    assert draft.status == "validation_failed"
    assert any(issue["issue_type"] == "node_contract_gap" for issue in draft.issues)
    # The compiled path terminates with a structured contract gap; whole-graph
    # regeneration and Blueprint rewriting are not recovery routes.
    monkeypatch.setattr(api, "_regenerate_responsibility_graph", AsyncMock(side_effect=AssertionError("forbidden")))
    monkeypatch.setattr(api, "_replan_blueprint_for_graph_closure", AsyncMock(side_effect=AssertionError("forbidden")))
    assert api._regenerate_responsibility_graph.await_count == 0
    assert api._replan_blueprint_for_graph_closure.await_count == 0


@pytest.mark.asyncio
async def test_invalid_endpoint_candidate_is_rejected_without_mutating_nodes():
    from backend.services.creator.responsibility_graph import GraphDraft, InputBinding, compile_responsibility_graph

    items = [_item("scripts/a.py", inputs=(), outputs=("text",)),
             _item("scripts/b.py", inputs=("value",), outputs=("markdown",))]
    draft = GraphDraft.freeze(items, input_bindings=[
        InputBinding("scripts/b.py", "value", "script_output", "scripts/missing.py", "value")])
    before = list(draft.function_items)
    await compile_responsibility_graph(draft)
    assert draft.status == "validation_failed"
    assert draft.function_items == before
    assert not draft.compiled_edges or all(edge["from_node"] != "scripts/missing.py" for edge in draft.compiled_edges)


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
