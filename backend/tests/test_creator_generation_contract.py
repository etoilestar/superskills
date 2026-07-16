from backend.services.creator.generation import _script_local_contract_payload, build_available_tool_context
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

    assert context["available_tools"][0]["input_schema"]["required"] == ["stale"]
    assert context["resolved_tools"][0]["signature"] == "lookup_value(query: str) -> dict"
    assert context["resolved_tools"][0]["input_schema"]["required"] == ["query"]
    assert context["resolved_tools"][0]["output_schema"]["required"] == ["source_value"]
    assert all("other_lookup" not in card for card in context["tool_function_cards"])
    assert context["allowed_function_imports"] == ["lookup_value"]
    assert "lookup_value" in context["tool_snippet_prompt"]
