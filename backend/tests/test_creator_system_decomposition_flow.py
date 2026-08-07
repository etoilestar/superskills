import json

import pytest

from backend.services.creator import api


def _request(**kwargs):
    data = {"user_request": "complete user goal", "human_feedback": "", "model": None}
    data.update(kwargs)
    return api.PreparePlanRequest(**data)


def _script_block(path: str, *, purpose: str, inputs: list[str], outputs: list[str]) -> str:
    return (
        f"- path: `{path}`\n"
        "  role: generic_script\n"
        f"  purpose: {purpose}\n"
        f"  inputs: [{', '.join(inputs)}]\n"
        f"  outputs: [{', '.join(outputs)}]\n"
        "  dependencies: []\n"
        "  required_capabilities: []\n"
        "  forbidden_capabilities: []\n"
        "  references: []\n"
        "  constraints: []"
    )


def _blueprint() -> str:
    return f"""## 📋 Skill 架构蓝图
### 基本信息
- **Skill 名称**: demo-skill
### I/O 契约
- **输入**: runtime text
- **输出**: text
- **触发词**: process
### 目录结构
[当前 Skill 根目录]
├── SKILL.md
└── scripts
    ├── a.py
    └── b.py
### 工作流逻辑
1. A 完成第一个原子执行子目标
2. B 使用 A 的结果完成第二个原子执行子目标
### SkillPlan / 文件职责计划
- path: `SKILL.md`
  role: skill_overview
  inputs: []
  outputs: []
  dependencies: []
  required_capabilities: []
  forbidden_capabilities: []
  references: []
  constraints: []
{_script_block('scripts/a.py', purpose='atomic subgoal 1', inputs=['input_1'], outputs=['output_1'])}
{_script_block('scripts/b.py', purpose='atomic subgoal 2', inputs=['input_1'], outputs=['output_1'])}
### 宿主执行方式
- **直接回答**: 否
- **需要脚本/命令**: 是
- **禁止隐式执行**: 遵守
- **执行后回答**: 返回 text
### 资源清单
- [ ] 无
"""


def _allocation(requirement_id: str, owners: list[str]) -> dict:
    return {
        "requirement_id": requirement_id,
        "requirement": f"requirement-{requirement_id}",
        "owners": owners,
        "evidence": {"responsibility": "planner evidence", "outputs": [], "capabilities": []},
    }


def test_requirement_ownership_validator_checks_only_reference_closure():
    items = [{"target_file": "scripts/unit_a.py"}]
    issues = api.collect_requirement_ownership_issues(
        requirement_channels={"R1": "executable", "R2": "direct", "R3": "resource"},
        requirement_allocations=[
            _allocation("R1", []),
            _allocation("R2", ["scripts/not_exists.py", "scripts/not_exists.py"]),
        ],
        frozen_function_items=items,
    )
    assert [(issue["code"], issue["requirement_id"]) for issue in issues] == [
        ("unowned_executable_requirement", "R1"),
        ("non_executable_requirement_owner", "R2"),
        ("unknown_requirement_owner", "R2"),
        ("duplicate_requirement_owner", "R2"),
        ("unknown_requirement_owner", "R2"),
        ("unknown_requirement_allocation", "R3"),
    ]
    assert api.collect_requirement_ownership_issues(
        requirement_channels={"R1": "executable", "R2": "direct"},
        requirement_allocations=[
            _allocation("R1", ["scripts/unit_a.py"]), _allocation("R2", []),
        ], frozen_function_items=items,
    ) == []


@pytest.mark.asyncio
async def test_requirement_ownership_repair_accepts_non_executable_without_owner():
    current = [_allocation("R1", [])]
    captured = {}

    async def model(messages, _model):
        captured["prompt"] = messages[0]["content"]
        return json.dumps({
            "requirement_allocations": current,
            "requirement_channels": {"R1": "direct"},
        })

    repaired = await api.repair_requirement_ownership(
        original_user_goal="g", frozen_blueprint=_blueprint(),
        frozen_function_items=[{"target_file": "scripts/unit_a.py"}],
        current_requirement_channels={"R1": "executable"},
        current_requirement_allocations=current,
        ownership_issues=[{"code": "unowned_executable_requirement", "requirement_id": "R1"}],
        model="p", model_call=model,
    )
    assert repaired["requirement_channels"] == {"R1": "direct"}
    assert repaired["requirement_allocations"][0]["owners"] == []
    assert "assign arbitrary owners merely to pass validation" in " ".join(captured["prompt"].split())
    assert "Every existing requirement allocation must remain present exactly once" in " ".join(captured["prompt"].split())
    assert "Do not add or remove requirement allocations." in captured["prompt"]
    assert "may be added or removed" not in captured["prompt"]


@pytest.mark.asyncio
async def test_requirement_ownership_repair_rejects_unaffected_requirement_change():
    before = [_allocation("R1", ["scripts/unit_a.py"]), _allocation("R2", [])]

    async def model(_messages, _model):
        return json.dumps({
            "requirement_allocations": [
                {**before[0], "evidence": {**before[0]["evidence"], "responsibility": "changed"}},
                _allocation("R2", ["scripts/unit_a.py"]),
            ],
            "requirement_channels": {"R1": "executable", "R2": "executable"},
        })

    with pytest.raises(api.RequirementOwnershipError) as raised:
        await api.repair_requirement_ownership(
            original_user_goal="g", frozen_blueprint=_blueprint(),
            frozen_function_items=[{"target_file": "scripts/unit_a.py"}],
            current_requirement_channels={"R1": "executable", "R2": "executable"},
            current_requirement_allocations=before,
            ownership_issues=[{"code": "unowned_executable_requirement", "requirement_id": "R2"}],
            model="p", model_call=model,
        )
    assert raised.value.code == "requirement_ownership_repair_scope_violation"


@pytest.mark.asyncio
async def test_requirement_ownership_repair_rejects_function_item_output():
    current = [_allocation("R1", [])]

    async def model(_messages, _model):
        return json.dumps({
            "requirement_allocations": [_allocation("R1", ["scripts/unit_a.py"])],
            "requirement_channels": {"R1": "executable"},
            "frozen_function_items": [{"target_file": "scripts/renamed.py"}],
        })

    with pytest.raises(api.RequirementOwnershipError) as raised:
        await api.repair_requirement_ownership(
            original_user_goal="g", frozen_blueprint=_blueprint(),
            frozen_function_items=[{"target_file": "scripts/unit_a.py"}],
            current_requirement_channels={"R1": "executable"},
            current_requirement_allocations=current,
            ownership_issues=[{"code": "unowned_executable_requirement", "requirement_id": "R1"}],
            model="p", model_call=model,
        )
    assert raised.value.code == "requirement_ownership_repair_protocol_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["add", "remove"])
async def test_requirement_ownership_repair_rejects_allocation_identity_changes(operation):
    before = [_allocation("R1", []), _allocation("R2", ["scripts/unit_a.py"])]
    after = (
        [*before, _allocation("R3", [])]
        if operation == "add"
        else [before[0]]
    )
    channels = {item["requirement_id"]: "executable" for item in after}

    async def model(_messages, _model):
        return json.dumps({
            "requirement_allocations": after,
            "requirement_channels": channels,
        })

    with pytest.raises(api.RequirementOwnershipError) as raised:
        await api.repair_requirement_ownership(
            original_user_goal="g", frozen_blueprint=_blueprint(),
            frozen_function_items=[{"target_file": "scripts/unit_a.py"}],
            current_requirement_channels={"R1": "executable", "R2": "executable"},
            current_requirement_allocations=before,
            ownership_issues=[{"code": "unowned_executable_requirement", "requirement_id": "R1"}],
            model="p", model_call=model,
        )
    assert raised.value.code == "requirement_ownership_repair_scope_violation"


@pytest.mark.asyncio
async def test_requirement_ownership_identity_mismatch_skips_model():
    calls = 0

    async def model(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("identity mismatch must not call repair")

    original = api.complete_creator_role_once
    api.complete_creator_role_once = model
    budget = api.RequirementOwnershipRepairBudget()
    try:
        with pytest.raises(api.RequirementOwnershipError) as raised:
            await api._validate_and_repair_requirement_ownership(
                request=_request(), blueprint_text=_blueprint(), function_items=[],
                projection={"requirement_allocations": [], "requirement_channels": {"R1": "executable"}},
                planner_model="p", repair_budget=budget,
            )
    finally:
        api.complete_creator_role_once = original
    assert calls == 0
    assert budget.attempts_used == 0
    assert raised.value.code == "requirement_ownership_repair_failed"
    assert raised.value.details["repair_error"]["code"] == "requirement_ownership_projection_identity_mismatch"


@pytest.mark.asyncio
async def test_request_level_ownership_budget_is_shared_across_validation_passes(monkeypatch):
    calls = 0
    repaired_projection = {
        "requirement_allocations": [_allocation("R1", ["scripts/unit_a.py"])],
        "requirement_channels": {"R1": "executable"},
    }

    async def model(_messages, _role, fallback_model=None):
        nonlocal calls
        calls += 1
        return json.dumps(repaired_projection)

    monkeypatch.setattr(api, "complete_creator_role_once", model)
    budget = api.RequirementOwnershipRepairBudget()
    invalid = {
        "requirement_allocations": [_allocation("R1", [])],
        "requirement_channels": {"R1": "executable"},
    }
    await api._validate_and_repair_requirement_ownership(
        request=_request(), blueprint_text=_blueprint(),
        function_items=[{"target_file": "scripts/unit_a.py"}], projection=invalid,
        planner_model="p", repair_budget=budget,
    )
    with pytest.raises(api.RequirementOwnershipError) as raised:
        await api._validate_and_repair_requirement_ownership(
            request=_request(), blueprint_text=_blueprint(),
            function_items=[{"target_file": "scripts/unit_a.py"}], projection=invalid,
            planner_model="p", repair_budget=budget,
        )
    assert calls == 1
    assert budget.attempts_used == 1
    assert raised.value.details["repair_error"]["code"] == "requirement_ownership_repair_budget_exhausted"


@pytest.mark.asyncio
async def test_valid_ownership_does_not_consume_request_budget(monkeypatch):
    async def model(*_args, **_kwargs):
        raise AssertionError("valid ownership must not call repair")

    monkeypatch.setattr(api, "complete_creator_role_once", model)
    budget = api.RequirementOwnershipRepairBudget()
    valid = {
        "requirement_allocations": [_allocation("R1", ["scripts/unit_a.py"])],
        "requirement_channels": {"R1": "executable"},
    }
    assert await api._validate_and_repair_requirement_ownership(
        request=_request(), blueprint_text=_blueprint(),
        function_items=[{"target_file": "scripts/unit_a.py"}], projection=valid,
        planner_model="p", repair_budget=budget,
    ) == valid
    assert budget.attempts_used == 0


@pytest.mark.asyncio
async def test_requirement_ownership_repair_protocol_error_is_wrapped(monkeypatch):
    calls = 0

    async def model(_messages, _role, fallback_model=None):
        nonlocal calls
        calls += 1
        return json.dumps({"unexpected": []})

    monkeypatch.setattr(api, "complete_creator_role_once", model)
    with pytest.raises(api.RequirementOwnershipError) as raised:
        await api._validate_and_repair_requirement_ownership(
            request=_request(), blueprint_text=_blueprint(),
            function_items=[{"target_file": "scripts/unit_a.py"}],
            projection={
                "requirement_allocations": [_allocation("R1", [])],
                "requirement_channels": {"R1": "executable"},
            },
            planner_model="p", repair_budget=api.RequirementOwnershipRepairBudget(),
        )
    assert calls == 1
    assert raised.value.code == "requirement_ownership_repair_failed"
    assert raised.value.details["repair_error"]["code"] == "requirement_ownership_repair_protocol_error"


@pytest.mark.asyncio
async def test_unknown_requirement_owner_is_repaired_once(monkeypatch):
    projection = {
        "requirement_allocations": [_allocation("R1", ["scripts/not_exists.py"])],
        "requirement_channels": {"R1": "executable"},
    }
    calls = 0

    async def model(_messages, _role, fallback_model=None):
        nonlocal calls
        calls += 1
        return json.dumps({
            "requirement_allocations": [_allocation("R1", ["scripts/unit_a.py"])],
            "requirement_channels": {"R1": "executable"},
        })

    monkeypatch.setattr(api, "complete_creator_role_once", model)
    repaired = await api._validate_and_repair_requirement_ownership(
        request=_request(), blueprint_text=_blueprint(),
        function_items=[{"target_file": "scripts/unit_a.py"}],
        projection=projection, planner_model="p",
        repair_budget=api.RequirementOwnershipRepairBudget(),
    )
    assert calls == 1
    assert repaired["requirement_allocations"][0]["owners"] == ["scripts/unit_a.py"]


@pytest.mark.asyncio
async def test_requirement_ownership_repair_fails_after_exactly_one_attempt(monkeypatch, caplog):
    projection = {
        "requirement_allocations": [_allocation("R1", [])],
        "requirement_channels": {"R1": "executable"},
    }
    calls = 0

    async def model(_messages, _role, fallback_model=None):
        nonlocal calls
        calls += 1
        return json.dumps(projection)

    monkeypatch.setattr(api, "complete_creator_role_once", model)
    with caplog.at_level("INFO"), pytest.raises(api.RequirementOwnershipError) as raised:
        await api._validate_and_repair_requirement_ownership(
            request=_request(), blueprint_text=_blueprint(),
            function_items=[{"target_file": "scripts/unit_a.py"}],
            projection=projection, planner_model="p",
            repair_budget=api.RequirementOwnershipRepairBudget(),
        )
    assert calls == 1
    assert raised.value.code == "requirement_ownership_repair_failed"
    assert raised.value.details["remaining_issues"][0]["code"] == "unowned_executable_requirement"
    assert "stage=initial" in caplog.text
    assert "stage=post_repair" in caplog.text
    assert "result=failed" in caplog.text


@pytest.mark.asyncio
async def test_requirement_prompts_define_ownership_closure(monkeypatch):
    prompts = []

    async def model(messages, _role, fallback_model=None):
        prompts.append(messages[0]["content"])
        if "requirement coverage projection" in messages[0]["content"]:
            return json.dumps({
                "requirement_allocations": [_allocation("R1", ["scripts/unit_a.py"])],
                "requirement_channels": {"R1": "executable"},
            })
        return json.dumps({"passed": True, "issues": [], "deferred_checks": []})

    monkeypatch.setattr(api, "complete_creator_role_once", model)
    await api._plan_requirement_allocations(
        request=_request(), blueprint_text=_blueprint(),
        function_items=[{"target_file": "scripts/unit_a.py"}], planner_model="p",
    )
    await api._review_blueprint_semantic_closure(
        request=_request(), blueprint_text=_blueprint(),
        function_items=[{"target_file": "scripts/unit_a.py"}],
        requirement_allocations=[_allocation("R1", ["scripts/unit_a.py"])],
        requirement_channels={"R1": "executable"}, planner_model="p",
    )
    assert "channel = executable\nowners = []" in prompts[0]
    assert "Every executable requirement must have at least one owner" in " ".join(prompts[0].split())
    assert "must not pass if an executable requirement has no owner" in prompts[1]
    assert "exact frozen FunctionItem target_file" in prompts[1]


@pytest.mark.asyncio
async def test_requirement_planner_receives_compact_clarification_context(monkeypatch):
    captured = {}

    async def model(messages, _role, fallback_model=None):
        captured.update(json.loads(messages[-1]["content"])["confirmed_user_context"])
        return json.dumps({
            "requirement_allocations": [_allocation("R1", [])],
            "requirement_channels": {"R1": "resource"},
        })

    monkeypatch.setattr(api, "complete_creator_role_once", model)
    request = _request(
        conversation_history=[
            {"role": "assistant", "content": "Choose constraint?"},
            {"role": "user", "content": "Use constraint x."},
        ],
        human_feedback="Keep the confirmed choice.",
    )
    await api._plan_requirement_allocations(
        request=request, blueprint_text=_blueprint(), function_items=[], planner_model="p",
    )
    assert captured["clarification_answers"] == [
        {"question": "Choose constraint?", "answer": "Use constraint x."}
    ]
    assert "Use constraint x." in captured["current_confirmed_goal"]
    assert captured["human_feedback"] == "Keep the confirmed choice."


@pytest.mark.asyncio
async def test_requirement_projection_protocol_repair_is_once_and_shape_only(monkeypatch):
    calls = []
    repaired = {
        "requirement_allocations": [
            _allocation("R1", []), _allocation("R2", ["scripts/unit_a.py"]),
        ],
        "requirement_channels": {"R1": "resource", "R2": "executable"},
    }

    async def model(messages, _role, fallback_model=None):
        calls.append(messages)
        if len(calls) == 1:
            return '{"requirement_allocations":[{"requirement_id":"R1"},"requirement_id":"R2"]}'
        payload = json.loads(messages[1]["content"])
        assert payload["raw_response"].endswith('"R2"]}')
        assert "required_schema" in payload
        assert "repairing only the JSON transport" in messages[0]["content"]
        return json.dumps(repaired)

    monkeypatch.setattr(api, "complete_creator_role_once", model)
    result = await api._plan_requirement_allocations(
        request=_request(), blueprint_text=_blueprint(),
        function_items=[{"target_file": "scripts/unit_a.py"}], planner_model="p",
    )
    assert result == repaired
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_requirement_projection_protocol_repair_does_not_loop(monkeypatch):
    calls = 0

    async def model(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return "not json"

    monkeypatch.setattr(api, "complete_creator_role_once", model)
    with pytest.raises(api.PreparePlanProtocolError, match="after one repair"):
        await api._plan_requirement_allocations(
            request=_request(), blueprint_text=_blueprint(),
            function_items=[], planner_model="p",
        )
    assert calls == 2


@pytest.mark.asyncio
async def test_prepare_main_path_reconciles_decomposition_then_interface_binds_graph(monkeypatch):
    blueprint = _blueprint()
    calls: list[str] = []
    interface_payloads: list[dict] = []
    endpoint_payloads: list[dict] = []
    review_responses = iter([
        {"passed": True, "issues": [], "deferred_checks": []},
    ])

    async def initial_planner(_messages, *_args, **_kwargs):
        calls.append("blueprint_planner")
        return json.dumps({
            "status": "ready",
            "clarifying_questions": [],
            "review_summary": {},
            "internal_blueprint_text": blueprint,
            "skill_name": "demo-skill",
            "blockers": [],
        })

    async def creator_model(messages, role, fallback_model=None):
        system = str(messages[0].get("content") or "")
        payload = json.loads(messages[-1]["content"])
        if "Skill Creator 模式" in system:
            calls.append("blueprint_planner")
            return json.dumps({
                "status": "ready",
                "clarifying_questions": [],
                "review_summary": {},
                "internal_blueprint_text": blueprint,
                "skill_name": "demo-skill",
                "blockers": [],
            })
        if "requirement coverage projection" in system:
            calls.append("requirement_allocation")
            return json.dumps({
                "requirement_allocations": [_allocation("R1", ["scripts/a.py"]), _allocation("R2", [])],
                "requirement_channels": {"R1": "executable", "R2": "executable"},
            })
        if "semantic coverage Reviewer" in system:
            calls.append("semantic_review")
            return json.dumps(next(review_responses))
        if "Repair only the listed requirement channel and ownership issues" in system:
            calls.append("requirement_ownership_repair")
            assert payload["affected_requirement_ids"] == ["R2"]
            return json.dumps({
                "requirement_allocations": [_allocation("R1", ["scripts/a.py"]), _allocation("R2", ["scripts/b.py"])],
                "requirement_channels": {"R1": "executable", "R2": "executable"},
            })
        if "reconciling a requirement allocation exactly once" in system:
            calls.append("allocation_reconciliation")
            return json.dumps({
                "requirement_allocations": [_allocation("R1", ["scripts/a.py"]), _allocation("R2", ["scripts/b.py"])],
                "requirement_channels": {"R1": "executable", "R2": "executable"},
            })
        if "planning semantic interfaces between already-frozen executable FunctionItems" in system:
            calls.append("interface_intent_planner")
            interface_payloads.append(payload)
            return json.dumps({
                "interfaces": [
                    {"interface_id": "I0001", "kind": "platform_to_member", "goal": "runtime input", "target_member": "scripts/a.py"},
                    {"interface_id": "I0002", "kind": "member_to_member", "goal": "handoff", "source_member": "scripts/a.py", "target_member": "scripts/b.py"},
                    {"interface_id": "I0003", "kind": "member_to_platform", "goal": "final output", "source_member": "scripts/b.py"},
                ]
            })
        if "Review an Interface Intent Plan" in system:
            calls.append("interface_semantic_review")
            return json.dumps({"passed": True, "issues": []})
        if "source_path" in system or "Return exactly one strict JSON object" in system:
            calls.append("endpoint_planner")
            endpoint_payloads.append(payload)
            obligation = payload["obligation"]
            if obligation["kind"] == "platform_to_script":
                assert "source_path" in system
                assert "never a script path" in system
                assert "Use []" in system
            if obligation["kind"] == "platform_to_script":
                return json.dumps({"source_id": payload["platform_inputs"][0]["slot_id"], "target_id": payload["target_member_inputs"][0]["input_id"], "source_path": []})
            if obligation["kind"] == "script_to_script":
                return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": payload["target_member_inputs"][0]["input_id"]})
            slot = next(value for value in payload["platform_outputs"] if value["field"] == "text")
            return json.dumps({"source_id": payload["source_member_outputs"][0]["output_id"], "target_id": slot["slot_id"]})
        raise AssertionError(f"unexpected model call for role={role}: {system[:120]}")

    monkeypatch.setattr(api, "complete_chat_once", initial_planner)
    monkeypatch.setattr(api, "complete_creator_role_once", creator_model)

    result = await api._generate_internal_blueprint_or_questions(_request())

    assert result["status"] == "ready"
    assert [item["target_file"] for item in result["function_items"]] == ["scripts/a.py", "scripts/b.py"]
    assert [item["inputs"] for item in result["function_items"]] == [["input_1"], ["input_1"]]
    assert [item["outputs"] for item in result["function_items"]] == [["output_1"], ["output_1"]]
    assert all(item["purpose"] for item in result["function_items"])
    assert result["function_items"][0]["purpose"] != result["function_items"][1]["purpose"]
    assert [edge["to_node"] for edge in result["responsibility_edges"]] == ["scripts/a.py", "scripts/b.py", "platform_output_node"]
    assert interface_payloads
    assert {item["target_file"] for item in interface_payloads[0]["function_items"]} == {"scripts/a.py", "scripts/b.py"}
    assert not {"subsystems", "subsystem_links", "members"} & set(interface_payloads[0])
    assert endpoint_payloads
    assert all("binding_candidates" not in payload for payload in endpoint_payloads)
    assert all("legacy_" + "goal_expansion" not in json.dumps(payload) for payload in endpoint_payloads)
    assert calls[:5] == [
        "blueprint_planner",
        "requirement_allocation",
        "requirement_ownership_repair",
        "semantic_review",
        "interface_intent_planner",
    ]
    assert calls[5] == "interface_semantic_review"
    assert calls.count("endpoint_planner") == 3


@pytest.mark.asyncio
async def test_bind_plan_repairs_overcomplete_interface_once(monkeypatch):
    initial_plan = {"interfaces": [{"interface_id": "I0003"}]}
    repaired_plan = {"interfaces": [{"interface_id": "I0002"}]}
    frozen_items = [
        {"target_file": "scripts/source.py"},
        {"target_file": "scripts/target.py"},
    ]
    final_edges = [
        {"from_node": "platform_input_node", "to_node": "scripts/source.py"},
        {"from_node": "scripts/source.py", "to_node": "scripts/target.py"},
        {"from_node": "scripts/target.py", "to_node": "platform_output_node"},
    ]
    expansion_plans = []
    repair_calls = []
    planning_calls = []

    monkeypatch.setattr(
        api,
        "_frozen_function_items_from_blueprint",
        lambda **_kwargs: frozen_items,
    )

    async def plan_interfaces(**_kwargs):
        planning_calls.append(_kwargs)
        return initial_plan

    async def expand_graph(**kwargs):
        expansion_plans.append(kwargs["interface_plan"])
        if len(expansion_plans) == 1:
            raise api.ResponsibilityGraphExpansionError(
                "interface has no remaining unbound target input",
                code="interface_plan_overcomplete",
                details={
                    "interface_id": "I0003",
                    "obligation_id": "O0003",
                    "kind": "script_to_script",
                    "source_member": "scripts/source.py",
                    "target_member": "scripts/target.py",
                    "reason": "no_remaining_target_endpoint",
                },
            )
        return final_edges

    async def repair_interfaces(**kwargs):
        repair_calls.append(kwargs)
        error = kwargs["validation_errors"][0]
        assert error["code"] == "interface_plan_overcomplete"
        assert error["details"]["interface_id"] == "I0003"
        assert "instruction" not in error
        assert kwargs["affected_members"] == ["scripts/source.py", "scripts/target.py"]
        assert kwargs["missing_platform_output_fields"] == []
        assert kwargs["system_requirements"] == planning_calls[0]["system_requirements"]
        return repaired_plan

    async def creator_model(*_args, **_kwargs):
        raise AssertionError("endpoint model should be handled by the expansion mock")

    monkeypatch.setattr(api, "plan_function_item_interfaces", plan_interfaces)
    monkeypatch.setattr(api, "expand_responsibility_graph", expand_graph)
    monkeypatch.setattr(api, "repair_interface_intents", repair_interfaces)
    monkeypatch.setattr(api, "complete_creator_role_once", creator_model)

    result = await api._bind_executable_responsibility_plan(
        request=_request(),
        current_planner_result={"internal_blueprint_text": _blueprint()},
        planner_model="p",
        allowed_function_item_targets=["scripts/source.py", "scripts/target.py"],
        requirement_allocations=[_allocation("R-system", [])],
        requirement_channels={"R-system": "direct"},
    )

    assert len(repair_calls) == 1
    assert expansion_plans == [initial_plan, repaired_plan]
    assert result["responsibility_edges"] == final_edges
    assert planning_calls[0]["system_requirements"] == [_allocation("R-system", [])]


@pytest.mark.asyncio
async def test_graph_revalidation_failure_is_wrapped_without_third_attempt(monkeypatch):
    plan_value = {"interfaces": []}
    monkeypatch.setattr(api, "_frozen_function_items_from_blueprint", lambda **_kwargs: [])
    monkeypatch.setattr(api, "plan_function_item_interfaces", lambda **_kwargs: _async_value(plan_value))
    attempts = 0

    async def expand_graph(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise api.ResponsibilityGraphExpansionError(
            "still incomplete", code="interface_plan_incomplete",
            details={"uncovered_inputs": []},
        )

    async def repair(**_kwargs):
        return plan_value

    monkeypatch.setattr(api, "expand_responsibility_graph", expand_graph)
    monkeypatch.setattr(api, "repair_interface_intents", repair)
    with pytest.raises(api.ResponsibilityGraphExpansionError) as raised:
        await api._bind_executable_responsibility_plan(
            request=_request(), current_planner_result={"internal_blueprint_text": _blueprint()},
            planner_model="p", allowed_function_item_targets=[],
        )
    assert attempts == 1
    assert raised.value.code == "interface_plan_incomplete"


@pytest.mark.asyncio
async def test_unknown_graph_error_bypasses_semantic_repair(monkeypatch):
    monkeypatch.setattr(api, "_frozen_function_items_from_blueprint", lambda **_kwargs: [])
    monkeypatch.setattr(api, "plan_function_item_interfaces", lambda **_kwargs: _async_value({"interfaces": []}))
    repair_called = False

    async def expand_graph(**_kwargs):
        raise api.ResponsibilityGraphExpansionError("cycle", code="cycle_error", details={})

    async def repair(**_kwargs):
        nonlocal repair_called
        repair_called = True

    monkeypatch.setattr(api, "expand_responsibility_graph", expand_graph)
    monkeypatch.setattr(api, "repair_interface_intents", repair)
    with pytest.raises(api.ResponsibilityGraphExpansionError) as raised:
        await api._bind_executable_responsibility_plan(
            request=_request(), current_planner_result={"internal_blueprint_text": _blueprint()},
            planner_model="p", allowed_function_item_targets=[],
        )
    assert raised.value.code == "cycle_error"
    assert repair_called is False


async def _async_value(value):
    return value
