import copy
from types import SimpleNamespace
import pytest

from backend.services.creator.generation import (
    _materialize_platform_skill_md_commands,
    _normalize_generated_file_content,
    _script_local_contract_payload,
    _validate_materialized_platform_skill_md_commands,
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


def test_platform_materializes_skill_md_commands_from_upstream_facts(monkeypatch, tmp_path):
    from backend.services.creator import generation

    entry = _entry(
        command_template="python scripts/main.py '{\"payload\":\"{{input}}\",\"limit\":3}'",
        default_values={"limit": 3},
    )
    monkeypatch.setattr(
        generation,
        "parse_blueprint",
        lambda _messages: SimpleNamespace(skill_plan=SimpleNamespace(files=[entry])),
    )
    monkeypatch.setattr(generation.settings, "skills_path", tmp_path)
    script_dir = tmp_path / "demo" / "scripts"
    script_dir.mkdir(parents=True)
    (script_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setattr(generation, "build_function_execution_context", lambda **_kw: {})
    monkeypatch.setattr(
        generation,
        "build_command_alignment_snapshot",
        lambda **_kw: {
            "target_keys": ["payload", "limit"],
            "required_target_keys": ["payload"],
            "confirmed_bindings": {"payload": "input"},
            "frozen_defaults": {"limit": 3},
        },
    )

    authored = "# Demo\n\n```bash\npython scripts/main.py '{\"wrong\":true}'\n```\n"
    result = _materialize_platform_skill_md_commands(
        authored,
        skill_name="demo",
        blueprint_text="ignored",
        responsibility_graph={},
    )

    assert '"wrong"' not in result
    assert result.count("```bash") == 1
    assert result.count("<!-- generated_by=contract_renderer -->") == 1
    assert "<!-- generated_by=contract_renderer -->\n```bash" in result
    assert "python scripts/main.py '{\"payload\":\"{{input}}\",\"limit\":3}'" in result
    assert '- defaults: {"limit": 3}' in result
    assert "## 运行命令" in result
    assert _materialize_platform_skill_md_commands(
        result,
        skill_name="demo",
        blueprint_text="ignored",
        responsibility_graph={},
    ) == result
    _validate_materialized_platform_skill_md_commands(
        result,
        skill_name="demo",
        blueprint_text="ignored",
    )
    with pytest.raises(ValueError, match="missing renderer ownership metadata"):
        _validate_materialized_platform_skill_md_commands(
            result.replace("<!-- generated_by=contract_renderer -->\n", ""),
            skill_name="demo",
            blueprint_text="ignored",
        )


def test_platform_replaces_model_authored_renderer_ownership_marker(monkeypatch):
    from backend.services.creator import generation

    entry = _entry(command_template="python scripts/main.py '{}'")
    monkeypatch.setattr(
        generation,
        "parse_blueprint",
        lambda _messages: SimpleNamespace(skill_plan=SimpleNamespace(files=[entry])),
    )

    authored = (
        "# Demo\n\n<!-- generated_by=contract_renderer -->\n"
        "```bash\npython scripts/fake.py '{}'\n```\n"
    )
    result = _materialize_platform_skill_md_commands(
        authored,
        skill_name="demo",
        blueprint_text="ignored",
    )

    assert "scripts/fake.py" not in result
    assert result.count("<!-- generated_by=contract_renderer -->") == 1
    assert "<!-- generated_by=contract_renderer -->\n```bash\npython scripts/main.py '{}'" in result


def test_required_resources_project_declared_dependencies_only_for_current_script():
    entry = _entry(
        path="scripts/a.py",
        dependencies=["references/rules.data", "assets/static.data"],
    )

    payload = _script_local_contract_payload(
        file_path=entry.path,
        purpose=entry.purpose,
        plan_entry=entry,
        stdout_schema={"type": "object", "required": ["result"], "properties": {"result": {}}},
    )

    assert payload["required_resources"] == ["references/rules.data", "assets/static.data"]
    assert payload["resource_refs"] == payload["required_resources"]


def test_required_resources_do_not_scan_blueprint_prose_or_other_script_entries():
    # Prose is intentionally absent from the frozen entry and therefore cannot
    # contaminate its contract.  A different script's declaration is likewise
    # not an input to this per-file projection.
    prose = "例如 references/example.data"
    other = _entry(path="scripts/b.py", dependencies=["references/b.data"], purpose=prose)
    current = _entry(path="scripts/a.py", dependencies=["references/a.data"], purpose=prose)

    current_payload = _script_local_contract_payload(
        file_path=current.path,
        purpose=current.purpose,
        plan_entry=current,
        stdout_schema={"type": "object", "required": ["result"], "properties": {"result": {}}},
    )
    other_payload = _script_local_contract_payload(
        file_path=other.path,
        purpose=other.purpose,
        plan_entry=other,
        stdout_schema={"type": "object", "required": ["result"], "properties": {"result": {}}},
    )

    assert current_payload["required_resources"] == ["references/a.data"]
    assert other_payload["required_resources"] == ["references/b.data"]
    assert "references/example.data" not in current_payload["required_resources"]


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


def test_script_prompt_deduplicates_equivalent_graph_and_tool_binding_representations():
    from backend.services.creator.generation import _build_script_generate_file_prompt_variant

    clear_registered_tool_capabilities()
    register_tool_capability(ToolCapability(
        name="capability_a",
        display_name="Capability A",
        category="test",
        roles=["generic_script"],
        functions=[ToolFunctionManifest(
            function_name="call_a",
            import_path="backend.services.runtime_tools",
            short_description="Call A.",
            when_to_use="Use call A.",
            signature="call_a(input_alpha: str) -> dict",
            input_schema={"type": "object", "required": ["input_alpha"]},
            output_schema={"type": "object", "required": ["result_gamma"]},
            return_contract="RETURN_SENTINEL_789",
            example_return="EXAMPLE_RETURN_SENTINEL",
            example_stdout="EXAMPLE_STDOUT_SENTINEL",
            common_mistakes=["COMMON_MISTAKE_SENTINEL"],
            required_secrets=["REQUIRED_SECRET_SENTINEL"],
        )],
    ))
    try:
        binding = {
            "available_tools": [{
                "tool_id": "capability_a.call_a",
                "capability_name": "capability_a",
                "function_name": "call_a",
            }],
            "dependencies": ["BINDING_SENTINEL_456"],
        }
        entry = {
            "path": "scripts/worker_a.py",
            "role": "generic_script",
            "file_type": "script",
            "purpose": "produce result_gamma",
            "runtime": "python",
            "language": "python",
            "inputs": ["input_alpha", "input_beta"],
            "outputs": ["result_gamma"],
            "dependencies": [],
            "required_capabilities": [],
            "tool_binding_summary": binding,
        }
        graph_context = {
            "function_item": {
                "must_do": ["use input_alpha"],
                "must_not_do": ["ignore input_beta"],
                "constraints": ["GRAPH_SENTINEL_123"],
                "inputs": ["input_alpha", "input_beta"],
                "outputs": ["result_gamma"],
            },
            "incoming_edges": [{"producer": "upstream_a"}],
            "outgoing_edges": [{"consumer": "downstream_b"}],
        }
        messages = _build_script_generate_file_prompt_variant(
            file_path="scripts/worker_a.py",
            skill_name="test_skill",
            purpose="produce result_gamma",
            blueprint_text="",
            role="generic_script",
            skill_plan_entry=entry,
            function_execution_context=graph_context,
            variant="standard",
        )
        prompt = "\n".join(str(message["content"]) for message in messages)
        local_contract = _script_local_contract_payload(
            file_path="scripts/worker_a.py",
            purpose="produce result_gamma",
            plan_entry=_entry(runtime_contract={"tool_binding_summary": binding}),
            stdout_schema={"type": "object", "required": ["result_gamma"]},
        )
        registry_tool = next(
            tool for tool in local_contract["resolved_tools"]
            if tool["tool_id"] == "capability_a.call_a"
        )

        assert prompt.count("GRAPH_SENTINEL_123") == 1
        assert prompt.count("BINDING_SENTINEL_456") == 1
        assert "capability_a.call_a" in prompt
        assert "call_a(input_alpha: str) -> dict" in prompt
        assert "RETURN_SENTINEL_789" in prompt
        assert "input_alpha" in prompt
        assert "result_gamma" in prompt
        assert len(local_contract["resolved_tools"]) == 2
        assert prompt.count('"tool_id"') >= len(local_contract["resolved_tools"])
        assert registry_tool["example_return"] == "EXAMPLE_RETURN_SENTINEL"
        assert registry_tool["example_stdout"] == "EXAMPLE_STDOUT_SENTINEL"
        assert registry_tool["common_mistakes"] == ["COMMON_MISTAKE_SENTINEL"]
        assert registry_tool["required_secrets"] == ["REQUIRED_SECRET_SENTINEL"]
        assert "EXAMPLE_RETURN_SENTINEL" in prompt
        assert "EXAMPLE_STDOUT_SENTINEL" in prompt
        assert "COMMON_MISTAKE_SENTINEL" in prompt
        assert "REQUIRED_SECRET_SENTINEL" not in prompt
    finally:
        clear_registered_tool_capabilities()


def test_stable_diffusion_helper_returns_manifest_file_outputs_in_trial_mode(tmp_path, monkeypatch):
    from backend.services.skill_runtime import generate_stable_diffusion_image

    monkeypatch.setenv("SKILL_TRIAL_RUN", "1")
    monkeypatch.setenv("IMAGE_MODEL", "fixture-image-model")

    result = generate_stable_diffusion_image(
        "A generic geometric illustration",
        output_dir=tmp_path,
    )

    assert result["file_outputs"] == [result["image_path"]]
    assert result["image_path"].endswith(".png")
