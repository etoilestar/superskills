import json

import pytest

from backend.services.creator.contracts import _check_reference_file_contract
from backend.services.creator.common import RequirementItem
from backend.services.creator import repair
from backend.services.skill_plan import SkillPlanEntry


def _reference(text: str) -> str:
    return "---\ntitle: Tiny\ndescription: Short legal reference.\n---\n" + text


def _entry():
    return SkillPlanEntry(
        path="scripts/main.py",
        role="generic_script",
        file_type="script",
        purpose="Transform the payload into the required result.",
        runtime="python",
        language="python",
        inputs=["payload"],
        outputs=["result"],
        dependencies=[],
        required_capabilities=[],
    )


def _req():
    return RequirementItem(
        target_file="scripts/main.py",
        role="generic_script",
        runtime="python",
        purpose="Transform the payload into the required result.",
        inputs=["payload"],
        outputs=["result"],
        must_do=["Return a result derived from the payload."],
    )


def _failed_ids(results):
    return {r.id for r in results if not r.passed}


def test_reference_backend_does_not_require_purpose_body_token_overlap():
    results = _check_reference_file_contract(
        "references/style.md",
        _reference("# Alpha\n\nCompletely valid body text about layout."),
        purpose="financial risk calculation thresholds",
    )

    assert "reference.content.covers_own_semantic_purpose" not in _failed_ids(results)
    assert "reference.content.has_reference_value" not in _failed_ids(results)
    assert not _failed_ids(results)


def test_short_legal_reference_backend_passes_without_length_or_heading_gate():
    results = _check_reference_file_contract(
        "references/tiny.md",
        _reference("ok"),
        purpose="Long business purpose unrelated to body tokens.",
    )

    assert not _failed_ids(results)


@pytest.mark.parametrize(
    "content,expected_failed",
    [
        ("---\ntitle: Bad\ndescription: Bad\n---\n", {"markdown.body.missing", "reference.metadata.body_exists", "reference.not_empty"}),
        ("---\ntitle: [\ndescription: Bad\n---\nbody", {"markdown.frontmatter.invalid_yaml", "markdown.frontmatter.yaml_parse_failed"}),
        (_reference("```python\nprint('unterminated')\n"), {"markdown.fences.unclosed", "reference.markdown.fences_balanced"}),
    ],
)
def test_reference_backend_fails_objective_markdown_format(content, expected_failed):
    results = _check_reference_file_contract("references/bad.md", content, purpose="anything")

    assert expected_failed & _failed_ids(results)


@pytest.mark.asyncio
async def test_python_pass_stub_reaches_model_judge_instead_of_static_responsibility_failure(monkeypatch):
    calls = {"judge": 0}

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(repair, "route_model", lambda *a, **k: Route())

    async def fake_complete(messages, model):
        calls["judge"] += 1
        return json.dumps({
            "passed": False,
            "blocking_issues": [{
                "issue_type": "semantic_action_incomplete",
                "semantic_failure": "pass body does not implement the FunctionItem",
                "problem": "empty implementation",
                "minimal_edit": "implement the function",
            }],
            "repair_instructions": "implement the function",
        })

    monkeypatch.setattr(repair, "complete_chat_once", fake_complete)

    result = await repair._run_script_responsibility_review(
        file_path="scripts/main.py",
        script_content="def run(payload):\n    pass\n",
        skill_plan_entry=_entry(),
        requirements=[],
        review_context={},
    )

    assert calls["judge"] == 1
    assert result["passed"] is False
    assert result.get("model") == "unit-test-model"


@pytest.mark.asyncio
async def test_python_missing_generic_variable_names_does_not_static_fail_before_judge(monkeypatch):
    calls = {"judge": 0}

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(repair, "route_model", lambda *a, **k: Route())

    async def fake_complete(messages, model):
        calls["judge"] += 1
        prompt = messages[-1]["content"]
        assert "current script FunctionItem" in prompt
        return json.dumps({"passed": True, "blocking_issues": [], "advisory_notes": []})

    monkeypatch.setattr(repair, "complete_chat_once", fake_complete)

    result = await repair._run_script_responsibility_review(
        file_path="scripts/main.py",
        script_content="def run(x):\n    return {'result': x}\n",
        skill_plan_entry=_entry(),
        requirements=[],
        review_context={},
    )

    assert calls["judge"] == 1
    assert result["passed"] is True


@pytest.mark.asyncio
async def test_writer_judge_shared_function_item_and_authorized_tool_contracts(monkeypatch):
    captured = {}

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(repair, "route_model", lambda *a, **k: Route())

    async def fake_complete(messages, model):
        captured.setdefault("prompts", []).append(messages[-1]["content"])
        rid = _req().id
        return json.dumps({
            "passed": True,
            "checks": [{"requirement_id": rid, "passed": True}],
            "blocking_issues": [],
        })

    monkeypatch.setattr(repair, "complete_chat_once", fake_complete)
    binding = {"primary_tool_ids": ["demo.tool"], "functions": [{"function_name": "demo"}]}

    await repair._run_script_responsibility_review(
        file_path="scripts/main.py",
        script_content="def run(payload):\n    return {'result': payload}\n",
        skill_plan_entry=_entry(),
        requirements=[_req()],
        review_context={
            "requirement_graph": {"function_items": [_req().model_dump()], "responsibility_edges": []},
            "current_file_tool_binding": binding,
        },
    )

    prompt = captured["prompts"][0]
    assert "当前文件 FunctionItem graph context" in prompt
    assert "当前文件已授权工具合同" in prompt
    assert "scripts/main.py" in prompt

from types import SimpleNamespace

from backend.services.creator.common import (
    ResponsibilityGraph,
    _platform_boundary_nodes,
    build_function_execution_context,
    validate_responsibility_graph_schema,
)
from backend.services.creator.generation import _script_local_contract_payload
from backend.services.creator.api import generate_file
from backend.services.creator import api as creator_api
from backend.services.creator import e2e as creator_e2e

def _graph(requirements=None, edges=None, *, include_input=True, include_output=True):
    platform_input, platform_output = _platform_boundary_nodes()
    return ResponsibilityGraph(
        requirements=requirements or [],
        platform_input_node=platform_input if include_input else {},
        platform_output_node=platform_output if include_output else {},
        dataflow_edges=edges if edges is not None else [
            {
                "from_node": "platform_input_node",
                "from_output": "text",
                "to_node": "scripts/main.py",
                "to_input": "payload",
                "purpose": "receive input",
                "constraints": [],
            },
            {
                "from_node": "scripts/main.py",
                "from_output": "result",
                "to_node": "platform_output_node",
                "to_input": "text",
                "purpose": "return output",
                "constraints": [],
            },
        ],
    )


def test_skill_md_generate_file_source_no_early_success_return_before_format_gate():
    import inspect
    source = inspect.getsource(generate_file)
    assert "skip SKILL.md static format/contract repair" not in source
    assert "skill_md_generation_failed" not in source
    assert source.index("_first_round_format_stage_error") < source.index("_validate_skill_md_blueprint_alignment")


def test_reference_semantic_review_helper_exists_and_uses_validator_task():
    import inspect
    source = inspect.getsource(repair._run_reference_semantic_review)
    assert "VALIDATOR_TASK" in source
    assert "token overlap" in source
    assert "passed" in source and "issues" in source and "repair_instructions" in source


def test_markdown_format_failures_use_full_rewrite_not_region_rewrite_in_main_path():
    import inspect
    source = inspect.getsource(generate_file)
    marker = 'prompt_variant="rewrite_markdown_full_format"'
    marker_pos = source.index(marker)
    start = source.rindex('request.file_path != "SKILL.md"', 0, marker_pos)
    end = source.index("continue", marker_pos)
    block = source[start:end]
    assert 'request.file_path != "SKILL.md"' in block
    assert "_is_markdown_creator_file(request.file_path)" in block
    assert "format_full_rewrite" in block
    assert "_build_markdown_format_full_rewrite_prompt" in block
    assert "format_region_rewrite" not in block
    assert "_build_markdown_region_rewrite_prompt" not in block


def test_python_compile_failures_use_full_rewrite_prompt_not_semantic_patch():
    import inspect
    source = inspect.getsource(generate_file)
    marker = "is_compile_rewrite_error ="
    block = source[source.index(marker):source.index('if error_source == "model_empty_content"', source.index(marker))]
    assert "_build_strict_compile_rewrite_prompt" in block
    assert "strict_compile_rewrite" in block
    assert "repair_mode" not in block


def test_semantic_patch_path_continues_to_next_loop_for_format_then_semantic():
    import inspect
    source = inspect.getsource(generate_file)
    repair_call = source.index("repaired_candidate = await _repair_generated_file_with_feedback")
    next_continue = source.index("continue", repair_call)
    next_format_gate = source.index("_first_round_format_stage_error")
    next_semantic_gate = source.index("_run_script_responsibility_review")
    assert next_continue > repair_call
    assert next_format_gate < next_semantic_gate


def test_writer_and_judge_use_json_equivalent_function_execution_context(monkeypatch):
    entry = _entry()
    binding = {"primary_tool_ids": ["script_argv_guard"]}
    graph = {"function_items": [_req().model_dump()], "responsibility_edges": []}

    writer_payload = _script_local_contract_payload(
        file_path="scripts/main.py",
        purpose=entry.purpose,
        plan_entry=entry,
        stdout_schema={"type": "object"},
        requirements=[_req()],
        responsibility_graph=graph,
    )
    writer_context = writer_payload["function_execution_context"]
    judge_context = build_function_execution_context(
        graph=graph,
        target_file="scripts/main.py",
        current_file_tool_binding=writer_payload["current_file_tool_binding"],
        fallback_function_item=_req(),
    )

    assert writer_context["function_item"] == judge_context["function_item"]
    assert writer_context["incoming_edges"] == judge_context["incoming_edges"]
    assert writer_context["outgoing_edges"] == judge_context["outgoing_edges"]
    assert writer_context["authorized_tool_contracts"] == judge_context["authorized_tool_contracts"]


def test_responsibility_graph_rejects_non_python_scripts_and_accepts_python():
    files = [SimpleNamespace(path="scripts/main.py", purpose="do x")]
    graph = _graph(requirements=[_req()])
    assert validate_responsibility_graph_schema(graph, files).function_items

    for bad in ("scripts/main.js", "scripts/run.sh"):
        bad_graph = _graph(requirements=[RequirementItem(target_file=bad, purpose="bad")])
        try:
            validate_responsibility_graph_schema(bad_graph, [SimpleNamespace(path=bad, purpose="bad")])
        except Exception as exc:
            assert "executable FilePlan scripts" in str(exc)
        else:
            raise AssertionError(f"{bad} should not be a FunctionItem target")


def test_responsibility_graph_rejects_function_item_outside_file_plan_scripts():
    graph = _graph(requirements=[_req()])
    with pytest.raises(Exception) as exc:
        validate_responsibility_graph_schema(graph, [SimpleNamespace(path="SKILL.md", purpose="docs")])
    assert getattr(exc.value, "code", "") == "validator_incomplete"


def test_scriptless_responsibility_graph_still_valid():
    graph = ResponsibilityGraph(requirements=[])
    assert validate_responsibility_graph_schema(graph, [SimpleNamespace(path="SKILL.md", purpose="docs")]).function_items == []


def test_scriptless_responsibility_graph_rejects_execution_edges():
    graph = _graph(requirements=[], edges=[
        {
            "from_node": "platform_input_node",
            "from_output": "text",
            "to_node": "scripts/main.py",
            "to_input": "payload",
        }
    ])
    with pytest.raises(Exception) as exc:
        validate_responsibility_graph_schema(graph, [SimpleNamespace(path="SKILL.md", purpose="docs")])
    assert getattr(exc.value, "code", "") == "responsibility_graph_platform_io_conflict"


def test_responsibility_graph_rejects_script_not_connected_to_platform_input():
    graph = _graph(requirements=[_req()], edges=[
        {
            "from_node": "scripts/main.py",
            "from_output": "result",
            "to_node": "platform_output_node",
            "to_input": "text",
            "constraints": [],
        }
    ])
    with pytest.raises(Exception) as exc:
        validate_responsibility_graph_schema(graph, [SimpleNamespace(path="scripts/main.py", purpose="do x")])
    assert getattr(exc.value, "code", "") == "responsibility_graph_platform_io_conflict"


def test_responsibility_graph_rejects_script_not_connected_to_platform_output():
    graph = _graph(requirements=[_req()], edges=[
        {
            "from_node": "platform_input_node",
            "from_output": "text",
            "to_node": "scripts/main.py",
            "to_input": "payload",
            "constraints": [],
        }
    ])
    with pytest.raises(Exception) as exc:
        validate_responsibility_graph_schema(graph, [SimpleNamespace(path="scripts/main.py", purpose="do x")])
    assert getattr(exc.value, "code", "") == "responsibility_graph_platform_io_conflict"


@pytest.mark.parametrize("target", ["SKILL.md", "references/guide.md", "assets/logo.png"])
def test_responsibility_graph_rejects_non_execution_resource_nodes(target):
    graph = _graph(
        requirements=[RequirementItem(target_file=target, purpose="not executable")],
        edges=[],
    )
    with pytest.raises(Exception) as exc:
        validate_responsibility_graph_schema(graph, [SimpleNamespace(path=target, purpose="not executable")])
    assert getattr(exc.value, "code", "") == "validator_incomplete"


def test_e2e_source_does_not_call_semantic_judge_or_toolpool_planning():
    import inspect
    source = inspect.getsource(creator_e2e)
    forbidden = [
        "_run_script_responsibility_review(",
        "_run_reference_semantic_review(",
        "_plan_tool_pool_patch_from_responsibility_feedback(",
        "explore_tool_pool(",
        "rediscover_for_repair=True",
    ]
    for token in forbidden:
        assert token not in source

from backend.services.skill_plan import normalize_structured_function_items


def _structured_function_item(path):
    return {
        "target_file": path,
        "role": "generic_script",
        "purpose": "do work",
        "inputs": [],
        "outputs": [],
        "required_capabilities": [],
        "constraints": [],
    }

def test_normalize_structured_function_items_is_python_only():
    assert normalize_structured_function_items([_structured_function_item("scripts/main.py")])[0]["target_file"] == "scripts/main.py"
    for bad in ("scripts/main.js", "scripts/run.sh", "references/ref.md"):
        try:
            normalize_structured_function_items([_structured_function_item(bad)])
        except ValueError as exc:
            assert "must_be_scripts_python" in str(exc)
        else:
            raise AssertionError(f"{bad} should not normalize as a FunctionItem")



def test_writer_and_judge_reuse_provided_context_without_rebuilding(monkeypatch):
    entry = _entry()
    shared_context = {
        "function_item": {"target_file": "scripts/main.py", "purpose": "shared"},
        "incoming_edges": [],
        "outgoing_edges": [],
        "authorized_tool_contracts": [],
    }
    build_calls = {"writer": 0, "judge": 0}

    def fail_writer_build(*args, **kwargs):
        build_calls["writer"] += 1
        raise AssertionError("writer should reuse provided function_execution_context")

    def fail_judge_build(*args, **kwargs):
        build_calls["judge"] += 1
        raise AssertionError("judge should reuse provided function_execution_context")

    monkeypatch.setattr("backend.services.creator.generation.build_function_execution_context", fail_writer_build)
    monkeypatch.setattr(repair, "build_function_execution_context", fail_judge_build)

    writer_payload = _script_local_contract_payload(
        file_path="scripts/main.py",
        purpose=entry.purpose,
        plan_entry=entry,
        stdout_schema={"type": "object"},
        requirements=[_req()],
        responsibility_graph={"function_items": [_req().model_dump()], "responsibility_edges": []},
        function_execution_context=shared_context,
    )
    assert writer_payload["function_execution_context"] == shared_context

    async def fake_complete(messages, model):
        assert "shared" in messages[-1]["content"]
        return json.dumps({"passed": True, "blocking_issues": [], "advisory_notes": []})

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(repair, "route_model", lambda *a, **k: Route())
    monkeypatch.setattr(repair, "complete_chat_once", fake_complete)

    import asyncio
    result = asyncio.run(repair._run_script_responsibility_review(
        file_path="scripts/main.py",
        script_content="def run(payload):\n    return {'result': payload}\n",
        skill_plan_entry=entry,
        requirements=[],
        review_context={"function_execution_context": shared_context},
    ))
    assert result["passed"] is True
    assert build_calls == {"writer": 0, "judge": 0}


def test_judge_rebuilds_context_when_toolpool_context_not_provided(monkeypatch):
    calls = {"judge": 0}
    rebuilt_context = {
        "function_item": {"target_file": "scripts/main.py", "purpose": "rebuilt_after_toolpool_patch"},
        "incoming_edges": [],
        "outgoing_edges": [],
        "authorized_tool_contracts": [{"tool_id": "new.tool", "functions": []}],
    }

    def fake_build(*args, **kwargs):
        calls["judge"] += 1
        return rebuilt_context

    async def fake_complete(messages, model):
        assert "rebuilt_after_toolpool_patch" in messages[-1]["content"]
        assert "new.tool" in messages[-1]["content"]
        return json.dumps({"passed": True, "blocking_issues": [], "advisory_notes": []})

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(repair, "build_function_execution_context", fake_build)
    monkeypatch.setattr(repair, "route_model", lambda *a, **k: Route())
    monkeypatch.setattr(repair, "complete_chat_once", fake_complete)

    import asyncio
    result = asyncio.run(repair._run_script_responsibility_review(
        file_path="scripts/main.py",
        script_content="def run(payload):\n    return {'result': payload}\n",
        skill_plan_entry=_entry(),
        requirements=[],
        review_context={"current_file_tool_binding": {"primary_tool_ids": ["new.tool"]}},
    ))
    assert result["passed"] is True
    assert calls["judge"] == 1


def test_is_python_function_item_target_single_implementation_source():
    import inspect
    import backend.services.skill_plan as skill_plan_module
    import backend.services.creator.common as common_module

    assert common_module.is_python_function_item_target is skill_plan_module.is_python_function_item_target
    assert inspect.getsourcefile(common_module.is_python_function_item_target) == inspect.getsourcefile(skill_plan_module.is_python_function_item_target)


def test_initial_function_execution_context_uses_current_skill_binding_payload():
    import inspect
    source = inspect.getsource(generate_file)
    helper_start = source.index("def _build_current_function_execution_context")
    helper_end = source.index("function_execution_context: dict[str, Any] | None = _build_current_function_execution_context()", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "context_binding: dict[str, Any] = dict(current_skill_binding_payload)" in helper_source
    assert "current_file_tool_binding=context_binding" in helper_source
    assert "load_tool_pool(settings.skills_path / skill_name)" not in helper_source
    assert "get_file_binding(context_tool_pool, request.file_path)" not in helper_source


def test_toolpool_augmentation_rebuilds_function_execution_context_immediately():
    import inspect
    source = inspect.getsource(generate_file)
    marker = "tool_re_explore_count += 1"
    start = source.index(marker)
    block = source[start:source.index("except Exception as planning_exc", start)]
    assert "function_execution_context = _build_current_function_execution_context()" in block
    assert "function_execution_context = None" not in block


@pytest.mark.asyncio
async def test_repair_model_uses_new_canonical_context_instead_of_old_writer_tool_context(monkeypatch):
    captured = {}

    async def fake_request_and_apply(**kwargs):
        captured["task_context"] = kwargs["task_context"]
        return ({}, kwargs["current_content"].replace("old_call", "new_call"), {"changed_lines": 1})

    monkeypatch.setattr(repair, "_request_and_apply_repair_patch", fake_request_and_apply)
    monkeypatch.setattr(repair, "_apply_deterministic_micro_patch_if_safe", lambda **kwargs: None)
    monkeypatch.setattr(repair, "_creator_tool_context_for_script", lambda **kwargs: "fresh_tool_context")
    monkeypatch.setattr(repair, "resolve_tool_snippets_for_context", lambda **kwargs: [])
    monkeypatch.setattr(repair, "tool_snippet_prompt", lambda snippets: "")

    old_prompt_messages = [{"role": "user", "content": "old_writer_tool_context should not leak"}]
    new_context = {
        "function_item": {"target_file": "scripts/main.py", "purpose": "new canonical"},
        "incoming_edges": [],
        "outgoing_edges": [],
        "authorized_tool_contracts": [{"tool_id": "fresh.tool", "functions": []}],
    }

    result = await repair._repair_generated_file_with_feedback(
        prompt_messages=old_prompt_messages,
        model="unit-test-model",
        file_path="scripts/main.py",
        previous_content="def run():\n    old_call()\n",
        validation_error="script_requirement_failed",
        targeted_repair="use new tool",
        skill_plan_entry={"path": "scripts/main.py", "role": "generic_script"},
        current_file_binding={"primary_tool_ids": ["fresh.tool"]},
        tool_pool_summary={"bindings": ["fresh.tool"]},
        function_execution_context=new_context,
    )

    assert "new_call" in result
    assert "fresh.tool" in captured["task_context"]
    assert "new canonical" in captured["task_context"]
    assert "old_writer_tool_context" not in captured["task_context"]


def test_skill_md_and_markdown_rewrite_prompts_use_input_json_position_arg_wording():
    from backend.services.creator import generation

    generation_messages = generation._build_generate_file_prompt(
        file_path="SKILL.md",
        skill_name="demo",
        purpose="demo skill",
        blueprint_text="目录结构:\n- scripts/main.py\n",
        conversation_history=[],
    )
    generation_prompt = "\n".join(str(message.get("content") or "") for message in generation_messages)

    rewrite_messages = creator_api._build_markdown_format_full_rewrite_prompt(
        file_path="SKILL.md",
        skill_name="demo",
        blueprint_text="目录结构:\n- scripts/main.py\n",
        deterministic_error="command JSON quotes broken",
        current_content="```bash\npython scripts/main.py '{bad json}'\n```",
    )
    rewrite_prompt = "\n".join(str(message.get("content") or "") for message in rewrite_messages)

    for prompt in (generation_prompt, rewrite_prompt):
        assert "输入 JSON" in prompt
        assert "第一个位置参数" in prompt
        assert "sys.argv[1]" in prompt


@pytest.mark.asyncio
async def test_reference_semantic_review_normalizes_string_issues(monkeypatch):
    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(repair, "route_model", lambda *a, **k: Route())

    async def fake_complete(messages, model):
        return json.dumps({
            "passed": False,
            "issues": [
                "reference 内容与自身职责不一致",
                "reference 错误承担了 executable script 职责",
            ],
            "repair_instructions": "只修改正文语义内容",
        })

    monkeypatch.setattr(repair, "complete_chat_once", fake_complete)

    result = await repair._run_reference_semantic_review(
        file_path="references/example.md",
        content="# Example\n\nBody",
        purpose="example reference",
    )

    assert result["passed"] is False
    assert len(result["issues"]) == 2
    assert all(isinstance(issue, dict) for issue in result["issues"])
    assert result["issues"][0]["failed_file"] == "references/example.md"
    assert result["issues"][0]["reason"] == "reference 内容与自身职责不一致"


@pytest.mark.asyncio
async def test_reference_semantic_review_preserves_dict_issues(monkeypatch):
    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(repair, "route_model", lambda *a, **k: Route())

    async def fake_complete(messages, model):
        return json.dumps({
            "passed": False,
            "issues": [{
                "id": "custom.reference.issue",
                "reason": "内容跑题",
                "minimal_edit": "修正正文",
                "extra_field": "preserve me",
            }],
        })

    monkeypatch.setattr(repair, "complete_chat_once", fake_complete)

    result = await repair._run_reference_semantic_review(
        file_path="references/example.md",
        content="# Example\n\nBody",
        purpose="example reference",
    )

    issue = result["issues"][0]
    assert issue["id"] == "custom.reference.issue"
    assert issue["reason"] == "内容跑题"
    assert issue["minimal_edit"] == "修正正文"
    assert issue["failed_file"] == "references/example.md"
    assert issue["extra_field"] == "preserve me"


@pytest.mark.asyncio
async def test_reference_semantic_review_generates_fallback_issue_when_empty(monkeypatch):
    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(repair, "route_model", lambda *a, **k: Route())

    async def fake_complete(messages, model):
        return json.dumps({
            "passed": False,
            "issues": [],
            "reason": "reference semantic failure",
            "repair_instructions": "patch body",
        })

    monkeypatch.setattr(repair, "complete_chat_once", fake_complete)

    result = await repair._run_reference_semantic_review(
        file_path="references/example.md",
        content="# Example\n\nBody",
        purpose="example reference",
    )

    assert result["passed"] is False
    assert result["issues"][0]["id"] == "reference_semantic.failed"
    assert result["issues"][0]["failed_file"] == "references/example.md"


def test_reference_generate_file_branch_does_not_wrap_script_exception():
    import inspect

    source = inspect.getsource(generate_file)
    start = source.index('elif request.file_path.startswith("references/"):')
    end = source.index('elif request.file_path.startswith("scripts/"):', start)
    reference_block = source[start:end]

    assert "ScriptFunctionalValidationError" not in reference_block
    assert 'source="reference_semantic_failed"' in reference_block
    assert 'layer="semantic"' in reference_block


def test_generate_file_prefers_persisted_file_binding_before_skill_projection():
    import inspect
    source = inspect.getsource(generate_file)
    start = source.index('if request.file_path.startswith("scripts/"):')
    block = source[start:source.index('if request.file_path.startswith("scripts/"):', start + 1)]
    assert "current_file_binding = get_file_binding" in block
    assert block.index("current_file_binding = get_file_binding") < block.index("get_skill_tool_binding")
