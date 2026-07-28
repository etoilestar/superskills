import json
import inspect

import pytest

from backend.services.creator import api, contracts
from backend.services.blueprint_parser import parse_files_from_blueprint
from backend.services.creator.common import (
    _authoritative_blueprint_skill_paths,
    _paths_requiring_skill_md_mentions,
)
from backend.services.platform_io_contract import build_platform_io_contract
from backend.services.skill_plan import (
    GraphValidationError,
    structured_responsibility_graph_input_provenance_gaps,
    validate_structured_responsibility_edge_transport,
    SkillPlan,
    SkillPlanEntry,
    normalize_skill_plan,
    capabilities_for_role,
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


def test_strict_fileplan_preserves_placeholder_like_declared_script_names():
    blueprint = """### SkillPlan / 文件职责计划
- path: `scripts/test.py`
  purpose: run
- path: `scripts/demo.py`
  purpose: run
- path: `scripts/example.py`
  purpose: run
"""
    files, _warnings = parse_files_from_blueprint(blueprint, strict=True)
    assert {file.path for file in files} == {
        "scripts/test.py", "scripts/demo.py", "scripts/example.py",
    }


@pytest.mark.parametrize("purpose", ["生成 输出 template static runtime", "plain prose"])
def test_strict_resource_classification_ignores_purpose_words(purpose):
    entry = SkillPlanEntry(
        path="assets/template.png", role="any-display-hint", purpose=purpose,
        file_type="asset", asset_source="bundled",
    )
    normalized = normalize_skill_plan(
        SkillPlan(skill_name="authority", files=[entry]), strict=True
    )
    assert [(item.path, item.file_type, item.asset_source) for item in normalized.files] == [
        ("assets/template.png", "asset", "bundled")
    ]


def test_role_names_do_not_define_capability_permissions():
    assert capabilities_for_role("image_generator") == capabilities_for_role("generic_script") == ([], [])


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


def test_frozen_default_resolves_input_without_platform_edge():
    items = [_function("scripts/a.py", ["arg_A", "arg_B=3"], ["result"])]
    edges = [_edge("platform_input_node", "user_request", "scripts/a.py", "arg_A")]
    assert structured_responsibility_graph_input_provenance_gaps(items, edges) == []
    context = api._build_responsibility_graph_construction_context(
        frozen_blueprint_text="", allowed_function_item_targets=["scripts/a.py"],
        function_items=items, responsibility_edges=edges,
    )
    assert context["node_contracts"][0]["frozen_defaults"] == {"arg_B": 3}
    assert [domain["target_input"] for domain in context["input_source_domains"]] == ["arg_A"]


def test_graph_rejects_external_provenance_for_frozen_default():
    items = [_function("scripts/a.py", ["arg_B=3"], ["result"])]
    edges = [_edge("platform_input_node", "user_request", "scripts/a.py", "arg_B")]
    with pytest.raises(Exception, match="provenance_class_conflict"):
        validate_structured_responsibility_edge_transport(edges, function_items=items)


def test_blueprint_freeze_preserves_typed_structured_defaults():
    blueprint = """## 📋 Skill 架构蓝图
### 基本信息
- **Skill 名称**: typed-defaults
### I/O 契约
- **输入**: user_request
- **输出**: result
### 目录结构
- SKILL.md
- scripts/: `scripts/a.py`
- references/: 无需创建
- assets/: 无需创建
### 工作流逻辑
1. Run the worker.
### SkillPlan / 文件职责计划
- path: `SKILL.md`
  role: skill_overview
  purpose: usage
  inputs: []
  outputs: []
  dependencies: [scripts/a.py]
  required_capabilities: []
  business_forbidden_capabilities: []
  references: []
- path: `scripts/a.py`
  role: worker
  purpose: run
  inputs: [arg_A, count, enabled, title]
  outputs: [result]
  default_values: [count=3, enabled=true, title="demo"]
  dependencies: []
  required_capabilities: []
  business_forbidden_capabilities: []
  references: []
### 宿主执行方式
- **直接回答**: no
- **需要脚本/命令**: scripts/a.py
- **禁止隐式执行**: yes
- **执行后回答**: result
### 资源清单
- none
"""
    items = api._frozen_function_items_from_blueprint(
        frozen_blueprint_text=blueprint,
        allowed_function_item_targets=["scripts/a.py"],
    )
    assert items[0]["inputs"] == ["arg_A", "count", "enabled", "title"]
    assert items[0]["default_values"] == {"count": 3, "enabled": True, "title": "demo"}
    edges = [_edge("platform_input_node", "user_request", "scripts/a.py", "arg_A")]
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
    assert context["node_contracts"] == [{
        "node": "scripts/a.py",
        "inputs": ["value"],
        "outputs": ["result"],
    }]
    assert "scripts/extra.py" not in json.dumps(context)
    assert context["platform_input_contract"]["input_fields"]
    assert context["platform_output_contract"]["final_output_fields"]
    domain = context["input_source_domains"][0]
    assert (domain["target_file"], domain["target_input"]) == ("scripts/a.py", "value")
    assert {tuple(source.values()) for source in domain["legal_sources"]} == {
        *(('platform_input_node', field) for field in context["platform_input_contract"]["input_fields"]),
        ("scripts/a.py", "result"),
    }


def test_blueprint_prompt_includes_lightweight_runtime_contract_self_check():
    source = inspect.getsource(api._generate_internal_blueprint_or_questions)
    assert "lightweight runtime-contract self-check" in source
    assert "creation-time fixed、default 或 static configuration" in source
    assert "不生成或描述具体 ResponsibilityEdge" in source
    assert "不要为了“可能有用”额外创造 input 或 output" in source


@pytest.mark.asyncio
async def test_localized_repair_rejects_readonly_function_items_in_output(monkeypatch):
    item = _function("scripts/a.py", ["value"], ["result"])

    async def repair(*args, **kwargs):
        changed = {**item, "inputs": ["other"]}
        return json.dumps({"function_items": [changed], "responsibility_edges": []})

    monkeypatch.setattr(api, "complete_creator_role_once", repair)
    with pytest.raises(ValueError, match="only responsibility_edges"):
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


def test_graph_validation_error_carries_structured_endpoint_facts():
    items = [_function("scripts/a.py", [], ["result"])]
    with pytest.raises(GraphValidationError) as caught:
        validate_structured_responsibility_edge_transport(
            [_edge("scripts/a.py", "result", "unknown", "value")],
            function_items=items,
        )
    assert caught.value.code == "invalid_graph_endpoint"
    assert caught.value.details == {"edge_index": 0, "to_node": "unknown"}


@pytest.mark.parametrize(
    ("edges", "code", "detail"),
    [
        (None, "invalid_edge_schema", ("actual_type", "null")),
        ([{"bad": "shape"}], "invalid_edge_schema", ("invalid_edges", None)),
        ([_edge("platform_output_node", "result", "scripts/a.py", "value")], "invalid_graph_endpoint", ("from_node", "platform_output_node")),
    ],
)
def test_all_deterministic_graph_transport_failures_are_structured(edges, code, detail):
    items = [_function("scripts/a.py", ["value"], ["result"])]
    with pytest.raises(GraphValidationError) as caught:
        validate_structured_responsibility_edge_transport(edges, function_items=items)
    assert caught.value.code == code
    assert detail[0] in caught.value.details
    if detail[1] is not None:
        assert caught.value.details[detail[0]] == detail[1]


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
