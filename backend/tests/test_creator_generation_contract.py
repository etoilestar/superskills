import copy

from backend.services.creator.generation import (
    _normalize_generated_file_content,
    _script_local_contract_payload,
    build_available_tool_context,
)
from backend.services.creator.contracts import detect_markdown_hard_format_failures
from backend.services.creator_tool_registry import (
    ToolCapability,
    ToolFunctionManifest,
    clear_registered_tool_capabilities,
    register_tool_capability,
)
from backend.services.skill_plan import SkillPlanEntry


def _entry(**kw):
    data = dict(
        path="scripts/main.py",
        role="generic_script",
        file_type="script",
        purpose="test",
        runtime="python",
        language="python",
        inputs=["payload"],
        outputs=["result"],
        dependencies=[],
        required_capabilities=[],
    )
    data.update(kw)
    return SkillPlanEntry(**data)


def test_reference_normalization_strips_only_complete_outer_markdown_wrapper():
    content = "```markdown\n# Title\n\n正文\n```\n"

    assert _normalize_generated_file_content("references/guide.md", content) == "# Title\n\n正文"


def test_reference_normalization_preserves_internal_fenced_code_block():
    content = "# Title\n\n```python\nprint('hello')\n```\n"

    assert _normalize_generated_file_content("references/guide.md", content) == content.strip()


def test_reference_normalization_leaves_incomplete_outer_wrapper_for_validator():
    content = "```markdown\n# Title\n\n```python\nprint('hello')\n```\n"

    normalized = _normalize_generated_file_content("references/guide.md", content)

    assert normalized == content.strip()
    failures = detect_markdown_hard_format_failures("references/guide.md", normalized, False)
    assert any(failure["id"] == "markdown.fences.unclosed" for failure in failures)


def test_script_and_skill_normalization_behaviors_remain_path_specific():
    assert _normalize_generated_file_content("scripts/a.py", "```python\nprint('ok')\n```") == "print('ok')"
    assert _normalize_generated_file_content("SKILL.md", "```markdown\n# Skill\n```") == "# Skill"


def test_script_local_contract_merges_structured_tool_binding_with_explicit_values():
    clear_registered_tool_capabilities()
    register_tool_capability(ToolCapability(
        name="custom_lookup",
        display_name="Custom Lookup",
        category="retrieval",
        roles=["generic_script"],
        dependencies=[{"package": "rich", "imports": ["rich"]}],
        functions=[ToolFunctionManifest(
            function_name="lookup_value",
            import_path="backend.services.runtime_tools.custom_tools.lookup",
            short_description="Lookup a value.",
            when_to_use="Use for lookup.",
            signature="lookup_value(query: str) -> dict",
            input_schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
            output_schema={"type": "object", "required": ["source_value"], "properties": {"source_value": {"type": "string"}}},
            required_capabilities=["custom_lookup"],
        )],
    ))
    try:
        entry = _entry(
            raw_capability_hints=["custom_lookup"],
            runtime_contract={
                "tool_binding_summary": {
                    "primary_tool_ids": ["custom_lookup"],
                    "available_tools": [{
                        "tool_id": "custom_lookup",
                        "function_name": "lookup_value",
                        "import_path": "backend.services.runtime_tools.custom_tools.lookup",
                        "input_schema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
                        "output_schema": {"type": "object", "required": ["source_value"], "properties": {"source_value": {"type": "string"}}},
                    }],
                    "dependencies": ["explicit_dep"],
                }
            },
        )
        payload = _script_local_contract_payload(
            file_path="scripts/main.py",
            purpose="test",
            plan_entry=entry,
            stdout_schema={"type": "object", "required": ["result"], "properties": {"result": {"type": "string"}}},
        )

        binding = payload["current_file_tool_binding"]
        assert binding["primary_tool_ids"] == ["custom_lookup", "script_argv_guard"]
        assert binding["allowed_import_paths"] == ["backend.services.runtime_tools.custom_tools.lookup", "backend.services.runtime_tools"]
        assert binding["allowed_function_imports"] == [
            "lookup_value",
            "strict_json_argv_guard",
        ]
        assert binding["dependencies"] == ["explicit_dep"]
        assert payload["allowed_helper_imports"] == ["lookup_value", "strict_json_argv_guard"]
        assert payload["available_tools"] == binding["available_tools"]
    finally:
        clear_registered_tool_capabilities()


def test_script_local_contract_autofills_runtime_helper_and_argv_guard_when_binding_empty():
    clear_registered_tool_capabilities()
    register_tool_capability(ToolCapability(
        name="runtime_lookup",
        display_name="Runtime Lookup",
        category="retrieval",
        roles=["generic_script"],
        functions=[ToolFunctionManifest(
            function_name="lookup_value",
            import_path="backend.services.runtime_tools",
            short_description="Lookup a value.",
            when_to_use="Use for lookup.",
            signature="lookup_value(query: str) -> dict",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            required_capabilities=["runtime_lookup"],
        )],
    ))
    try:
        entry = _entry(
            raw_capability_hints=["runtime_lookup"],
            runtime_contract={
                "tool_binding_summary": {
                    "primary_tool_ids": ["runtime_lookup"],
                    "available_tools": [{
                        "tool_id": "runtime_lookup",
                        "function_name": "lookup_value",
                        "import_path": "backend.services.runtime_tools",
                        "input_schema": {"type": "object"},
                        "output_schema": {"type": "object"},
                    }]
                }
            },
        )
        payload = _script_local_contract_payload(
            file_path="scripts/main.py",
            purpose="test",
            plan_entry=entry,
            stdout_schema={"type": "object", "required": ["result"], "properties": {"result": {"type": "string"}}},
        )

        binding = payload["current_file_tool_binding"]
        assert binding["allowed_helper_imports"] == ["lookup_value", "strict_json_argv_guard"]
        assert binding["allowed_import_paths"] == ["backend.services.runtime_tools"]
        assert binding["allowed_function_imports"] == [
            "lookup_value",
            "strict_json_argv_guard",
        ]
        assert "runtime_lookup" in binding["primary_tool_ids"]
        assert "script_argv_guard" in binding["primary_tool_ids"]
    finally:
        clear_registered_tool_capabilities()


def test_script_local_contract_uses_binding_available_tools_as_only_index():
    clear_registered_tool_capabilities()
    register_tool_capability(ToolCapability(
        name="bound_lookup",
        display_name="Bound Lookup",
        category="retrieval",
        roles=["generic_script"],
        functions=[ToolFunctionManifest(
            function_name="lookup_value",
            import_path="backend.services.runtime_tools.custom_tools.lookup",
            short_description="Lookup a value.",
            when_to_use="Use for lookup.",
            signature="lookup_value(query: str) -> dict",
            input_schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
            output_schema={"type": "object"},
            required_capabilities=["bound_lookup"],
        )],
    ))
    entry = _entry(
        required_capabilities=["unbound_capability"],
        runtime_contract={
            "implementation_resolution": {"selected_tools": ["extra_tool"]},
            "tool_binding_summary": {
                "available_tools": [{
                    "tool_id": "bound_lookup.lookup_value",
                    "capability_name": "bound_lookup",
                    "function_name": "lookup_value",
                    "import_path": "backend.services.runtime_tools.custom_tools.lookup",
                    "signature": "lookup_value(query: str) -> dict",
                    "input_schema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
                    "output_schema": {"type": "object"},
                }],
                "allowed_import_paths": ["backend.services.runtime_tools.custom_tools.extra"],
                "allowed_function_imports": ["extra_tool"],
                "allowed_helper_imports": ["extra_tool"],
            },
        },
    )

    payload = _script_local_contract_payload(
        file_path="scripts/main.py",
        purpose="test",
        plan_entry=entry,
        stdout_schema={"type": "object", "required": ["result"], "properties": {"result": {"type": "string"}}},
    )

    tool_ids = [tool["tool_id"] for tool in payload["available_tools"]]
    assert tool_ids == ["bound_lookup.lookup_value", "script_argv_guard"]
    assert payload["current_file_tool_binding"]["available_tools"] == payload["available_tools"]
    assert payload["current_file_tool_binding"]["allowed_function_imports"] == ["lookup_value", "strict_json_argv_guard"]
    assert "extra_tool" not in payload["current_file_tool_binding"]["allowed_function_imports"]


def test_available_tool_context_resolves_registry_facts_and_ignores_stale_index_schema():
    clear_registered_tool_capabilities()
    register_tool_capability(ToolCapability(
        name="multi_lookup",
        display_name="Multi Lookup",
        category="retrieval",
        roles=["generic_script"],
        functions=[
            ToolFunctionManifest(
                function_name="lookup_value",
                import_path="backend.services.runtime_tools.custom_tools.lookup",
                short_description="Lookup a value.",
                when_to_use="Use for lookup.",
                signature="lookup_value(query: str) -> dict",
                input_schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
                output_schema={"type": "object", "required": ["source_value"], "properties": {"source_value": {"type": "string"}}},
                example_call="lookup_value(query='x')",
                required_capabilities=["multi_lookup"],
            ),
            ToolFunctionManifest(
                function_name="other_lookup",
                import_path="backend.services.runtime_tools.custom_tools.lookup",
                short_description="Other lookup.",
                when_to_use="Use for other lookup.",
                signature="other_lookup(value: str) -> dict",
                input_schema={"type": "object", "required": ["value"]},
                output_schema={"type": "object"},
                required_capabilities=["multi_lookup"],
            ),
        ],
    ))

    context = build_available_tool_context({
        "available_tools": [{
            "tool_id": "multi_lookup.lookup_value",
            "function_name": "lookup_value",
            "input_schema": {"type": "object", "required": ["stale"]},
        }]
    }, role="generic_script", file_path="scripts/main.py")

    assert set(context["available_tools"][0]) == {"tool_id", "capability_name", "function_name"}
    assert "input_schema" not in context["available_tools"][0]
    assert "signature" not in context["available_tools"][0]
    assert "call_template" not in context["available_tools"][0]
    assert context["resolved_tools"][0]["signature"] == "lookup_value(query: str) -> dict"
    assert context["resolved_tools"][0]["input_schema"]["required"] == ["query"]
    assert context["resolved_tools"][0]["output_schema"]["required"] == ["source_value"]
    assert all("other_lookup" not in card for card in context["tool_function_cards"])
    assert context["allowed_function_imports"] == ["lookup_value"]
    assert "lookup_value" in context["tool_snippet_prompt"]


def test_available_tool_context_fails_when_registry_index_cannot_resolve():
    clear_registered_tool_capabilities()
    try:
        build_available_tool_context({
            "available_tools": [{
                "tool_id": "missing_capability.missing_function",
                "function_name": "missing_function",
            }]
        })
    except ValueError as exc:
        assert "BOUND_AVAILABLE_TOOL_REGISTRY_RESOLUTION_FAILED" in str(exc)
        assert "missing_capability.missing_function" in str(exc)
    else:
        raise AssertionError("Expected unresolved available_tools index to fail")


def test_python_script_core_probe_is_visible_as_pure_index_and_resolved_from_registry():
    clear_registered_tool_capabilities()
    entry = _entry(runtime_contract={"tool_binding_summary": {"available_tools": []}})

    payload = _script_local_contract_payload(
        file_path="scripts/main.py",
        purpose="test",
        plan_entry=entry,
        stdout_schema={"type": "object", "required": ["result"], "properties": {"result": {"type": "string"}}},
    )

    assert {
        "tool_id": "script_argv_guard",
        "capability_name": "script_argv_guard",
        "function_name": "strict_json_argv_guard",
    } in payload["available_tools"]
    probe = next(tool for tool in payload["resolved_tools"] if tool["function_name"] == "strict_json_argv_guard")
    assert probe["import_path"] == "backend.services.runtime_tools"
    assert "strict_json_argv_guard" in probe["signature"]


def test_script_local_contract_runtime_contract_available_tools_uses_prompt_index_without_mutating_original():
    clear_registered_tool_capabilities()
    register_tool_capability(ToolCapability(
        name="lookup",
        display_name="Lookup",
        category="retrieval",
        roles=["generic_script"],
        functions=[ToolFunctionManifest(
            function_name="lookup_value",
            import_path="backend.services.runtime_tools.custom_tools.lookup",
            short_description="Lookup a value.",
            when_to_use="Use for lookup.",
            signature="lookup_value(query: str) -> dict",
            input_schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
            output_schema={"type": "object"},
            required_capabilities=["lookup"],
        )],
    ))
    runtime_contract = {
        "tool_binding_summary": {
            "available_tools": [{
                "tool_id": "lookup.lookup_value",
                "capability_name": "lookup",
                "function_name": "lookup_value",
                "input_schema": {"required": ["stale"]},
                "signature": "stale_signature()",
            }],
            "primary_tool_ids": ["lookup"],
            "dependencies": ["kept-for-compat"],
        }
    }
    original_runtime_contract = copy.deepcopy(runtime_contract)
    entry = _entry(runtime_contract=runtime_contract)

    payload = _script_local_contract_payload(
        file_path="scripts/main.py",
        purpose="test",
        plan_entry=entry,
        stdout_schema={"type": "object", "required": ["result"], "properties": {"result": {"type": "string"}}},
    )

    assert (
        payload["available_tools"]
        == payload["current_file_tool_binding"]["available_tools"]
        == payload["runtime_contract"]["tool_binding_summary"]["available_tools"]
    )
    for tool in payload["available_tools"]:
        assert set(tool) == {"tool_id", "capability_name", "function_name"}
    assert payload["resolved_tools"][0]["input_schema"]["required"] == ["query"]
    assert entry.runtime_contract == original_runtime_contract


def test_current_skill_binding_overrides_stale_guard_only_runtime_contract_with_registry_projection():
    from backend.services.creator import api

    catalog = api._creator_tool_catalog_for_planner()
    callable_tools = [
        tool for tool in catalog
        if tool.get("tool_id") != "script_argv_guard" and tool.get("functions")
    ]
    assert len(callable_tools) >= 2
    selected = callable_tools[:2]
    binding_tools = []
    for tool in selected:
        fn = tool["functions"][0]
        binding_tools.append({
            "tool_id": f"{tool['tool_id']}.{fn['function_name']}",
            "capability_name": tool["tool_id"],
            "function_name": fn["function_name"],
        })
    binding = {"available_tools": binding_tools}
    stale_entry = {
        "path": "scripts/main.py",
        "runtime_contract": {
            "tool_binding_summary": {
                "available_tools": [{
                    "tool_id": "script_argv_guard",
                    "capability_name": "script_argv_guard",
                    "function_name": "strict_json_argv_guard",
                }]
            }
        },
    }
    synced = api._with_current_skill_tool_binding(stale_entry, binding)
    entry = _entry(runtime_contract=synced["runtime_contract"])

    payload = _script_local_contract_payload(
        file_path="scripts/main.py",
        purpose="test",
        plan_entry=entry,
        stdout_schema={"type": "object"},
    )

    assert [tool["tool_id"] for tool in payload["available_tools"]] == [
        *[tool["tool_id"] for tool in binding_tools],
        "script_argv_guard",
    ]
    assert len(payload["resolved_tools"]) == len(payload["available_tools"])
    assert len(payload["tool_function_cards"]) > 1
    for tool in payload["resolved_tools"]:
        assert tool.get("import_path")
        assert tool.get("function_name")
        assert tool.get("signature")
    assert set(payload["current_file_tool_binding"]["allowed_import_paths"]) == {tool["import_path"] for tool in payload["resolved_tools"]}
    assert stale_entry["runtime_contract"]["tool_binding_summary"]["available_tools"][0]["tool_id"] == "script_argv_guard"


def test_script_prompt_projects_current_function_item_and_expands_only_relevant_tools():
    """Script A sees its graph contract and tool A's detail, not tool B's card."""
    from backend.services.creator.common import FunctionItem, ResponsibilityGraph, build_function_execution_context
    from backend.services.creator.generation import _build_script_generate_file_prompt_variant

    clear_registered_tool_capabilities()
    for capability, function_name in (("source_reader", "read_source"), ("unrelated_writer", "write_unrelated")):
        register_tool_capability(ToolCapability(
            name=capability,
            display_name=capability,
            category="test",
            roles=["generic_script"],
            functions=[ToolFunctionManifest(
                function_name=function_name,
                import_path=f"backend.services.runtime_tools.custom_tools.{capability}",
                short_description=f"{capability} helper",
                when_to_use=f"Use {capability}",
                signature=f"{function_name}(value: str) -> dict",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
            )],
        ))
    try:
        current = FunctionItem(
            target_file="scripts/a.py", purpose="produce paths", role="generic_script",
            inputs=["source_value", "count"], outputs=["result_paths"],
            required_tools=["source_reader"],
        )
        other = FunctionItem(
            target_file="scripts/b.py", purpose="unrelated", role="generic_script",
            inputs=["other_input"], outputs=["other_output"],
            required_tools=["unrelated_writer"],
        )
        graph = ResponsibilityGraph(
            requirements=[current, other],
            dataflow_edges=[
                {"from_node": "platform_input_node", "from_output": "source_value", "to_node": "scripts/a.py", "to_input": "source_value"},
                {"from_node": "scripts/a.py", "from_output": "result_paths", "to_node": "scripts/b.py", "to_input": "other_input"},
            ],
        )
        entry = _entry(
            inputs=current.inputs, outputs=current.outputs, required_capabilities=["source_reader"],
            runtime_contract={"tool_binding_summary": {"available_tools": [
                {"tool_id": "source_reader.read_source", "function_name": "read_source"},
                {"tool_id": "unrelated_writer.write_unrelated", "function_name": "write_unrelated"},
            ]}},
        )
        entry_payload = dict(entry.__dict__)
        entry_payload["tool_binding_summary"] = entry.runtime_contract["tool_binding_summary"]
        messages = _build_script_generate_file_prompt_variant(
            file_path="scripts/a.py", skill_name="demo", purpose=current.purpose,
            blueprint_text="", role="generic_script", skill_plan_entry=entry_payload,
            requirements=graph.requirements, responsibility_graph=graph,
            function_execution_context=build_function_execution_context(graph=graph, target_file="scripts/a.py"),
            variant="standard",
        )
        text = "\n".join(message["content"] for message in messages)
        assert "CURRENT SCRIPT CONTRACT" in text
        assert "source_value" in text and "result_paths" in text
        assert "不得改成同义词、别名" in text
        assert "read_source(value: str)" in text
        assert "write_unrelated(value: str)" not in text
        assert "other_output" not in text
    finally:
        clear_registered_tool_capabilities()
        from backend.services import creator_tool_registry
        creator_tool_registry._load_registered_tools_from_disk()
