import json
import inspect

import pytest

from backend.services.creator import api, contracts
from backend.services.creator.common import (
    _authoritative_blueprint_skill_paths,
    _paths_requiring_skill_md_mentions,
)
from backend.services.platform_io_contract import build_platform_io_contract
from backend.services.skill_plan import (
    structured_responsibility_graph_input_provenance_gaps,
    validate_structured_responsibility_edge_transport,
)


def _blueprint(extra_prose: str = "") -> str:
    return f"""## 📋 Skill 架构蓝图
### 基本信息
- **Skill 名称**: authority-test
### I/O 契约
- **输入**: text
- **输出**: text
### 目录结构
- SKILL.md
- scripts/: 无需创建
- references/: `references/real.md`
- assets/: 无需创建
### 工作流逻辑
1. Read the structured reference. {extra_prose}
### SkillPlan / 文件职责计划
- path: `SKILL.md`
  role: skill_overview
  purpose: usage
  inputs: []
  outputs: []
  dependencies: [references/real.md]
  required_capabilities: []
  business_forbidden_capabilities: []
  references: [references/real.md]
- path: `references/real.md`
  role: reference
  purpose: facts
  inputs: []
  outputs: []
  dependencies: []
  required_capabilities: []
  business_forbidden_capabilities: []
  references: []
### 宿主执行方式
- **直接回答**: answer
- **需要脚本/命令**: none
- **禁止隐式执行**: yes
- **执行后回答**: answer
### 资源清单
- references/real.md
"""


def _function(target, inputs, outputs):
    return {
        "target_file": target,
        "role": "worker",
        "purpose": "perform assigned responsibility",
        "inputs": inputs,
        "outputs": outputs,
        "required_capabilities": [],
        "constraints": [],
    }


def _edge(from_node, from_output, to_node, to_input, constraints=None):
    return {
        "from_node": from_node,
        "from_output": from_output,
        "to_node": to_node,
        "to_input": to_input,
        "purpose": "transport declared value",
        "constraints": constraints or [],
    }


def test_prose_path_is_not_authoritative_but_structured_file_is():
    paths = _authoritative_blueprint_skill_paths(
        _blueprint("A documentation illustration mentions references/example.md.")
    )
    assert [path for path in paths if path.startswith("references/")] == ["references/real.md"]


def test_prose_script_path_is_not_required_skill_md_mention():
    blueprint = _blueprint("Documentation mentions scripts/example.py without a file entry.")
    assert _paths_requiring_skill_md_mentions(blueprint, prefix="scripts/") == []


def test_structured_dependency_remains_in_authoritative_constraints():
    constraints = contracts._collect_blueprint_skillplan_constraints(
        blueprint_text=_blueprint(),
        skill_plan_entry={"dependencies": ["references/real.md"]},
    )
    assert constraints["declared_references"] == ["references/real.md"]


def test_required_input_missing_provenance_is_reported():
    items = [_function("scripts/a.py", [], ["x"]), _function("scripts/b.py", ["a", "b"], [])]
    edges = [_edge("scripts/a.py", "x", "scripts/b.py", "a")]
    validate_structured_responsibility_edge_transport(edges, function_items=items)
    assert structured_responsibility_graph_input_provenance_gaps(items, edges) == [("scripts/b.py", "b")]


def test_optional_platform_binding_with_explicit_default_resolves_input():
    boundary = build_platform_io_contract()["platform_skill_boundary"]
    root = boundary["preferred_structured_input_root"]
    items = [_function("scripts/a.py", ["limit"], [])]
    edges = [_edge("platform_input_node", root, "scripts/a.py", "limit", [{
        "type": "platform_parameter_binding", "source_key": "limit", "required": False, "default": 10,
    }])]
    validate_structured_responsibility_edge_transport(edges, function_items=items)
    assert structured_responsibility_graph_input_provenance_gaps(items, edges) == []


def test_platform_input_binding_resolves_input():
    boundary = build_platform_io_contract()["platform_skill_boundary"]
    root = boundary["preferred_structured_input_root"]
    items = [_function("scripts/a.py", ["value"], [])]
    edges = [_edge("platform_input_node", root, "scripts/a.py", "value", [{
        "type": "platform_parameter_binding", "source_key": "value", "required": True,
    }])]
    validate_structured_responsibility_edge_transport(edges, function_items=items)
    assert structured_responsibility_graph_input_provenance_gaps(items, edges) == []


def test_graph_construction_context_contains_only_frozen_structured_topology():
    item = _function("scripts/a.py", ["value"], ["result"])
    context = api._build_responsibility_graph_construction_context(
        frozen_blueprint_text="Prose mentions scripts/extra.py and an unrelated_input.",
        allowed_function_item_targets=["scripts/a.py"],
        function_items=[item],
        responsibility_edges=[],
    )
    assert context["allowed_function_targets"] == ["scripts/a.py"]
    assert context["function_items"] == [{
        "target_file": "scripts/a.py",
        "purpose": "perform assigned responsibility",
        "inputs": ["value"],
        "outputs": ["result"],
        "static_configuration": [],
    }]
    assert "scripts/extra.py" not in json.dumps(context)
    assert context["platform_input_contract"]["input_fields"]
    assert context["platform_output_contract"]["final_output_fields"]


def test_blueprint_prompt_includes_lightweight_runtime_contract_self_check():
    source = inspect.getsource(api._generate_internal_blueprint_or_questions)
    assert "lightweight runtime-contract self-check" in source
    assert "creation-time fixed、default 或 static configuration" in source
    assert "不生成或描述具体 ResponsibilityEdge" in source
    assert "不要为了“可能有用”额外创造 input 或 output" in source


@pytest.mark.asyncio
async def test_localized_repair_rejects_function_boundary_changes(monkeypatch):
    item = _function("scripts/a.py", ["value"], ["result"])

    async def repair(*args, **kwargs):
        changed = {**item, "inputs": ["other"]}
        return json.dumps({"function_items": [changed], "responsibility_edges": []})

    monkeypatch.setattr(api, "complete_creator_role_once", repair)
    with pytest.raises(ValueError, match="requires upstream FunctionItem replanning"):
        await api._repair_responsibility_graph_alignment(
            request=api.PreparePlanRequest(user_request="test"),
            frozen_blueprint_text="frozen",
            allowed_function_item_targets=["scripts/a.py"],
            function_items=[item],
            responsibility_edges=[],
            review_issues=[],
            planner_model="unit-test-model",
        )


def test_upstream_and_platform_binding_for_same_input_conflict():
    boundary = build_platform_io_contract()["platform_skill_boundary"]
    root = boundary["preferred_structured_input_root"]
    items = [_function("scripts/a.py", [], ["x"]), _function("scripts/b.py", ["value"], [])]
    edges = [
        _edge("scripts/a.py", "x", "scripts/b.py", "value"),
        _edge("platform_input_node", root, "scripts/b.py", "value", [{
            "type": "platform_parameter_binding", "source_key": "value", "required": True,
        }]),
    ]
    with pytest.raises(ValueError, match="conflicting_input_provenance.*target_input=value"):
        validate_structured_responsibility_edge_transport(edges, function_items=items)


@pytest.mark.asyncio
async def test_skill_reviewer_cannot_expand_authoritative_manifest(monkeypatch):
    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(contracts, "route_model", lambda *args, **kwargs: Route())

    async def review(*args, **kwargs):
        return json.dumps({
            "passed": True,
            "required_reference_paths": ["references/real.md", "references/extra.md"],
            "issues": [],
        })

    monkeypatch.setattr(contracts, "complete_creator_role_once", review)
    result = await contracts._review_skill_md_blueprint_intent_with_model(
        skill_name="authority-test",
        content="# Authority test",
        blueprint_text=_blueprint(),
        skill_plan_entry={},
    )
    assert result["required_reference_paths"] == ["references/real.md"]


@pytest.mark.asyncio
async def test_blocking_issue_cannot_bypass_authoritative_manifest(monkeypatch):
    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(contracts, "route_model", lambda *args, **kwargs: Route())

    async def review(*args, **kwargs):
        return json.dumps({
            "passed": False,
            "issues": [{
                "severity": "error",
                "blocking": True,
                "field": "resources",
                "message": "Missing references/extra.md",
                "expected": "Add references/extra.md",
                "minimal_edit": "Append references/extra.md",
            }],
            "repair_suggestions": "Add references/extra.md",
        })

    monkeypatch.setattr(contracts, "complete_creator_role_once", review)
    result = await contracts._review_skill_md_blueprint_intent_with_model(
        skill_name="authority-test",
        content="# Authority test",
        blueprint_text=_blueprint(),
        skill_plan_entry={},
    )
    assert result["passed"] is True
    assert result["issues"] == []
    assert result["repair_suggestions"] == ""
