import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.creator_tool_registry import (
    capabilities_for_role,
    get_role_pattern,
    get_script_roles,
    get_tool_capability,
    is_resource_role,
    set_tool_capability_override,
    is_script_role,
    list_tool_capabilities,
    tool_status,
    validate_capability_names,
)


def test_registry_exposes_builtin_creator_tools():
    names = {cap.name for cap in list_tool_capabilities()}

    assert "text_generation" in names
    assert "image_generation" in names
    assert "wechat_draft" in names
    assert "wechat_publish" in names
    assert get_tool_capability("wechat_publish").enabled_by_default is False
    assert get_tool_capability("wechat_publish").allow_external_side_effect is True


def test_role_capabilities_are_registry_driven():
    assert capabilities_for_role("text_generator") == (
        ["text_generation"],
        ["image_generation", "pdf_generation"],
    )
    assert capabilities_for_role("pdf_builder") == (["pdf_generation", "file_output"], [])
    assert capabilities_for_role("database_reader") == (["database_read"], [])


def test_roles_and_pattern_include_new_tool_roles():
    script_roles = set(get_script_roles())

    assert "vision_analyzer" in script_roles
    assert "search_reader" in script_roles
    assert "wechat_publisher" in script_roles
    assert "reference" not in script_roles
    assert "wechat_publisher" in get_role_pattern()


def test_validate_capability_names_reports_unknown_values():
    assert validate_capability_names(["text_generation", "missing_tool"]) == ["missing_tool"]


def test_tool_status_reports_missing_configuration(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    status = tool_status(get_tool_capability("database_read"))

    assert status["configured"] is False
    assert status["missing_secrets"] == ["DATABASE_URL"]


def test_role_kind_helpers_keep_resource_roles_out_of_script_roles():
    assert is_script_role("search_reader") is True
    assert is_script_role("reference") is False
    assert is_resource_role("reference") is True
    assert is_resource_role("database_reader") is False


def test_tool_status_reports_runtime_helper_availability_without_secret_values(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret-user:secret-pass@example/db")

    status = tool_status(get_tool_capability("database_read"))

    assert status["configured"] is True
    assert status["missing_secrets"] == []
    assert "postgresql://" not in str(status)
    assert status["missing_runtime_helpers"] == []
    assert "query_database_readonly" in status["runtime_helpers_available"]
    assert status["override_persistence"] == "process_memory"


def test_role_capabilities_filter_disabled_creator_tools_by_default():
    set_tool_capability_override("web_search", enabled=False, allow_creator_use=False)
    try:
        assert capabilities_for_role("search_reader") == ([], [])
        assert capabilities_for_role("search_reader", only_creator_enabled=False) == (["web_search"], [])
    finally:
        set_tool_capability_override("web_search", enabled=True, allow_creator_use=True)


def test_tool_status_reports_missing_runtime_dependencies(monkeypatch):
    monkeypatch.setattr("backend.services.creator_tool_registry._dependency_available", lambda dependency: False)

    status = tool_status(get_tool_capability("pdf_generation"))

    assert "create_pdf" in status["runtime_helpers_available"]
    assert status["missing_runtime_helpers"] == []
    assert status["missing_dependencies"] == ["reportlab"]


def test_resolve_tools_for_pdf_builder_exposes_preferred_helpers_without_forcing_them():
    from backend.services.creator_tool_registry import resolve_tools_for_skill_plan_entry

    entry = {
        "role": "pdf_builder",
        "required_capabilities": ["pdf_generation", "file_output"],
    }

    resolved = resolve_tools_for_skill_plan_entry(entry)

    assert "pdf_generation" in resolved.allowed_tools
    assert "create_pdf" in resolved.allowed_helper_imports
    assert "create_pdf_document" in resolved.allowed_helper_imports
    assert "build_pdf_report" not in resolved.allowed_helper_imports
    assert resolved.forbidden_imports == []
    assert "create_pdf" in resolved.tool_usage_prompt
    assert "usage_policy=helper_preferred" in resolved.tool_usage_prompt
    assert "不强制实现方式" in resolved.tool_usage_prompt


def test_resolve_tools_excludes_disabled_tool_from_prompt():
    from backend.services.creator_tool_registry import resolve_tools_for_skill_plan_entry

    set_tool_capability_override("pdf_generation", enabled=False, allow_creator_use=True)
    try:
        resolved = resolve_tools_for_skill_plan_entry({"role": "pdf_builder", "required_capabilities": ["pdf_generation"]})
        assert "pdf_generation" not in resolved.allowed_tools
        assert "create_pdf" not in resolved.allowed_helper_imports
        assert any("disabled" in warning for warning in resolved.warnings)
    finally:
        set_tool_capability_override("pdf_generation", enabled=True, allow_creator_use=True)


def test_registered_tool_resolve_uses_registered_helper_and_trial_dispatch(monkeypatch):
    from backend.services.creator_tool_registry import (
        ToolCapability,
        clear_registered_tool_capabilities,
        register_tool_capability,
        resolve_tools_for_skill_plan_entry,
    )
    from backend.services.skill_runtime import registered_tool_call

    clear_registered_tool_capabilities()
    monkeypatch.setenv("SKILL_TRIAL_RUN", "1")
    try:
        register_tool_capability(ToolCapability(
            name="fake_registered_lookup",
            display_name="Fake Registered Lookup",
            category="registered",
            roles=["fake_lookup_reader"],
            helper_imports=["registered_tool_call"],
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            trial_mode="mock",
            prompt_guidance="调用 registered_tool_call('fake_registered_lookup', payload)，不要直接 requests.post 未知 API。",
            forbidden_direct_imports=["requests.post", "urllib.request"],
            usage_policy="helper_required",
        ))

        resolved = resolve_tools_for_skill_plan_entry({
            "role": "fake_lookup_reader",
            "required_capabilities": ["fake_registered_lookup"],
        })

        assert resolved.allowed_tools == ["fake_registered_lookup"]
        assert resolved.allowed_helper_imports == ["registered_tool_call"]
        assert "requests.post" in resolved.forbidden_imports
        assert "registered_tool_call" in resolved.tool_usage_prompt
        assert registered_tool_call("fake_registered_lookup", {"query": "demo"})["tool_name"] == "fake_registered_lookup"
    finally:
        clear_registered_tool_capabilities()


def test_resolve_tool_snippets_prioritizes_error_repair_context():
    from backend.services.creator_tool_registry import resolve_tool_snippets_for_context, tool_snippet_prompt

    snippets = resolve_tool_snippets_for_context(
        role="pdf_builder",
        capabilities=["pdf_generation"],
        tool_names=["create_pdf"],
        file_path="scripts/build_pdf.py",
        failure_layer="final_platform_output_value_invalid",
        error_text="pdf_path exists but value is object; create_pdf result was wrapped",
        max_snippets=3,
    )

    assert snippets
    assert snippets[0]["tool"] == "pdf_generation"
    assert "create_pdf" in snippets[0]["code"]
    assert "Do not return {'pdf_path': result}" in snippets[0]["formatted"]
    assert "Snippets" in tool_snippet_prompt(snippets)


def test_office_and_table_capabilities_have_manifests_and_snippets():
    expected = {
        "docx_generation": "create_docx",
        "pptx_generation": "create_pptx",
        "xlsx_generation": "create_xlsx",
        "csv_generation": "create_csv",
        "docx_parsing": "read_docx_text",
        "pptx_parsing": "read_pptx_text",
        "spreadsheet_read": "read_spreadsheet",
        "csv_read": "read_csv",
    }
    names = {cap.name for cap in list_tool_capabilities()}
    assert set(expected).issubset(names)
    for capability_name, helper in expected.items():
        capability = get_tool_capability(capability_name)
        assert helper in capability.helper_imports
        assert capability.functions
        assert capability.snippets
        card = capability.functions[0]
        assert card.example_call and helper in card.example_call
        assert card.example_stdout
        assert card.common_mistakes
        snippet_text = capability.snippets[0].code + capability.snippets[0].return_rule
        assert helper in snippet_text
        assert "dict" in snippet_text or "JSON" in snippet_text
        if capability_name == "docx_generation":
            assert "text=" in snippet_text
            assert "blocks=" in snippet_text
            assert "paragraphs=" in snippet_text
            assert "title=" in snippet_text
            assert "output_filename" in snippet_text
        elif capability_name == "pptx_generation":
            assert "slides=" in snippet_text
            assert "blocks" in snippet_text
            assert "title=" in snippet_text
            assert "output_filename" in snippet_text
        elif capability_name == "xlsx_generation":
            assert "sheets=" in snippet_text
            assert "headers=" in snippet_text
            assert "rows=" in snippet_text
            assert "output_filename" in snippet_text
        elif capability_name == "csv_generation":
            assert "headers=" in snippet_text
            assert "rows=" in snippet_text
            assert "output_filename" in snippet_text
        if capability_name.endswith("_generation"):
            assert f"{capability_name.split('_')[0]}_path" in capability.output_schema["properties"]
            assert "file_paths" in capability.output_schema["properties"]
            assert "file_outputs" in capability.output_schema["properties"]
            assert "Do not drop file_outputs" in " ".join(card.common_mistakes)


def test_resolve_office_table_tools_does_not_route_to_pdf():
    from backend.services.creator_tool_registry import resolve_tools_for_skill_plan_entry

    for capability_name, helper in {
        "docx_generation": "create_docx",
        "pptx_generation": "create_pptx",
        "xlsx_generation": "create_xlsx",
        "csv_generation": "create_csv",
    }.items():
        resolved = resolve_tools_for_skill_plan_entry({"role": "composite_generator", "required_capabilities": [capability_name, "file_output"]})
        assert capability_name in resolved.allowed_tools
        assert helper in resolved.allowed_helper_imports
        assert "pdf_generation" not in resolved.allowed_tools
        assert "Do not route" in resolved.tool_usage_prompt


def _file_manifest(prop_schema):
    return {
        "runtime_type": "python_script",
        "tool_name": "sample_file_tool",
        "description": "sample",
        "input_schema": {"type": "object", "properties": {"file": prop_schema}},
        "output_schema": {"type": "object", "required": ["success"], "properties": {"success": {"type": "boolean"}, "seen": {}}},
        "dependencies": [],
        "permissions": {"network": False, "read_files": True, "write_files": False, "subprocess": False, "env": []},
        "artifact_policy": {"file_fields": []},
        "auth": {"required": "no", "secrets": []},
    }


def _make_sample_pdf(monkeypatch, tmp_path):
    import backend.services.creator_tool_registry as registry

    sample_pdf = tmp_path / "sample.pdf"
    sample_pdf.write_bytes(b"%PDF-1.4\n% test sample\n")
    monkeypatch.setattr(registry, "PDF_SAMPLE_PATH", sample_pdf)
    return registry, sample_pdf


def test_resolve_sample_input_pdf_content_media_type(monkeypatch, tmp_path):
    registry, sample_pdf = _make_sample_pdf(monkeypatch, tmp_path)

    manifest = _file_manifest({"type": "string", "format": "file-path", "contentMediaType": "application/pdf"})
    sample_input, notes = registry.resolve_tool_trial_sample_input(manifest, {})

    assert sample_input == {"file": str(sample_pdf.resolve())}
    assert notes == []


def test_resolve_sample_input_pdf_extension(monkeypatch, tmp_path):
    registry, sample_pdf = _make_sample_pdf(monkeypatch, tmp_path)

    manifest = _file_manifest({"type": "string", "format": "file-path", "accepted_extensions": [".pdf"]})
    sample_input, notes = registry.resolve_tool_trial_sample_input(manifest, {})

    assert sample_input["file"] == str(sample_pdf.resolve())
    assert notes == []


def test_resolve_sample_input_image_media_type_and_extension():
    from backend.services.creator_tool_registry import IMAGE_SAMPLE_PATH, resolve_tool_trial_sample_input

    for prop_schema in [
        {"type": "string", "format": "file-path", "contentMediaType": "image/png"},
        {"type": "string", "format": "file-path", "accepted_extensions": [".jpg"]},
    ]:
        sample_input, notes = resolve_tool_trial_sample_input(_file_manifest(prop_schema), {})
        assert sample_input["file"] == str(IMAGE_SAMPLE_PATH.resolve())
        assert notes == []


def test_resolve_sample_input_array_of_file_paths(monkeypatch, tmp_path):
    registry, sample_pdf = _make_sample_pdf(monkeypatch, tmp_path)

    manifest = _file_manifest({"type": "array", "items": {"type": "string", "format": "file-path", "contentMediaType": "application/pdf"}})
    sample_input, notes = registry.resolve_tool_trial_sample_input(manifest, {})

    assert sample_input == {"file": [str(sample_pdf.resolve())]}
    assert notes == []


def test_resolve_sample_input_does_not_override_user_value():
    from backend.services.creator_tool_registry import resolve_tool_trial_sample_input

    manifest = _file_manifest({"type": "string", "format": "file-path", "contentMediaType": "application/pdf"})
    sample_input, notes = resolve_tool_trial_sample_input(manifest, {"file": "/user/provided.pdf"})

    assert sample_input == {"file": "/user/provided.pdf"}
    assert notes == []


def test_resolve_sample_input_missing_sample_warns_without_fabricating(monkeypatch, tmp_path):
    import backend.services.creator_tool_registry as registry

    monkeypatch.setattr(registry, "PDF_SAMPLE_PATH", tmp_path / "missing.pdf")
    manifest = _file_manifest({"type": "string", "format": "file-path", "contentMediaType": "application/pdf"})
    sample_input, notes = registry.resolve_tool_trial_sample_input(manifest, {})

    assert sample_input == {}
    assert notes
    assert "missing" in notes[0]


def test_validate_direct_run_uses_raw_sample_input_without_autofill(monkeypatch, tmp_path):
    registry, _sample_pdf = _make_sample_pdf(monkeypatch, tmp_path)

    script = """
def run(payload, config=None):
    return {"success": True, "missing_file": "file" not in dict(payload or {})}
def sample_file_tool(payload, config=None):
    return run(payload, config)
"""
    validation = registry.validate_tool_manifest(
        _file_manifest({"type": "string", "format": "file-path", "contentMediaType": "application/pdf"}),
        adapter_code=script,
        sample_input={},
        dynamic=True,
        require_auth_config=False,
        direct_run=True,
    )

    assert validation["success"] is True
    assert validation["sample_input"] == {}
    assert validation["sample_notes"] == []
    assert validation["dynamic_trial"]["result"]["missing_file"] is True
    assert validation["dynamic_trial"]["temporary_environment"]["env"]["TOOL_TRIAL_RUN"] == "0"


def test_author_trial_run_and_finalize_return_resolved_sample_input(monkeypatch, tmp_path):
    import asyncio
    registry, sample_pdf = _make_sample_pdf(monkeypatch, tmp_path)

    async def fake_snippet(**kwargs):
        return None

    async def fake_contract(**kwargs):
        return {"sample_input": kwargs["sample_input"]}

    monkeypatch.setattr(registry, "_author_snippet_with_model", fake_snippet)
    monkeypatch.setattr(registry, "_summarize_tool_contract_with_model", fake_contract)

    manifest = _file_manifest({"type": "string", "format": "file-path", "contentMediaType": "application/pdf"})
    script = """
def run(payload, config=None):
    return {"success": True, "seen": payload}
def sample_file_tool(payload, config=None):
    return run(payload, config)
"""
    request = {"manifest": manifest, "runtime_code": script, "sample_input": {}}

    trial = asyncio.run(registry.author_tool({**request, "action": "trial_run"}))
    finalized = asyncio.run(registry.author_tool({**request, "action": "finalize"}))

    expected = {"file": str(sample_pdf.resolve())}
    assert trial["sample_input"] == expected
    assert finalized["sample_input"] == expected
    assert finalized["tool_contract"]["sample_input"] == expected


def test_single_sample_validation_failure_repairs_before_return(monkeypatch):
    import asyncio
    import backend.services.creator_tool_registry as registry

    manifest = registry.build_tool_manifest_draft({
        "tool_name": "repair_sample_tool",
        "description": "Echo payload after repair",
        "allowed_roles": ["generic_script"],
    })
    bad_script = """
def run(payload, config=None):
    raise RuntimeError('boom')
def repair_sample_tool(payload, config=None):
    return run(payload, config)
"""
    good_script = """
def run(payload, config=None):
    return {"success": True, "payload": payload}
def repair_sample_tool(payload, config=None):
    return run(payload, config)
"""
    calls = []

    async def fake_repair(**kwargs):
        calls.append(kwargs["validation"])
        return good_script

    monkeypatch.setattr(registry, "_repair_script_with_model", fake_repair)

    script_code, sample_input, validation, repair_log = asyncio.run(
        registry._validate_tool_manifest_with_repair(
            request={"description": "repair sample"},
            manifest=manifest,
            script_code=bad_script,
            sample_input={"q": "demo"},
            dynamic=True,
            real_run=False,
            require_auth_config=False,
            model_notes=[],
            warnings=[],
            max_attempts=3,
        )
    )

    assert calls
    assert repair_log
    assert validation["success"] is True
    assert validation["status"] == "validated_after_repair"
    assert sample_input == {"q": "demo"}
    assert "boom" not in script_code


def test_resolve_sample_input_autofills_required_schema_fields_when_frontend_sends_empty_object():
    from backend.services.creator_tool_registry import resolve_tool_trial_sample_input

    manifest = {
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "task_type": {"type": "string", "enum": ["summary", "rewrite"]},
                "count": {"type": "integer"},
            },
            "required": ["user_id", "task_type", "count"],
        }
    }

    sample_input, notes = resolve_tool_trial_sample_input(manifest, {})

    assert sample_input == {"user_id": "sample_user_id", "task_type": "summary", "count": 1}
    assert any("auto-filled from input_schema" in note for note in notes)


def test_no_additional_secrets_auth_reason_does_not_block_registration():
    from backend.services import creator_tool_registry as registry

    manifest = registry.build_tool_manifest_draft({
        "tool_name": "payload_identity_tool",
        "description": "Uses payload identity only.",
        "allowed_roles": ["generic_script"],
    })
    manifest["auth"] = {
        "required": "yes",
        "reason": "Authentication is handled via user_id and task_type in the request data; no additional secrets are required.",
        "secrets": [],
    }
    script = registry.generate_adapter_code(manifest)

    validation = registry.validate_tool_manifest(
        manifest,
        adapter_code=script,
        sample_input={},
        dynamic=False,
        require_auth_config=True,
    )

    assert validation["auth_gate"]["block_registration"] is False
    assert validation["can_register"] is True


def test_script_tool_boundary_reports_hallucinated_tool_call():
    from backend.services.creator.api import _script_tool_boundary_violations

    violations = _script_tool_boundary_violations(
        "result = run_registered_tool('missing_tool', {'q': 'demo'})",
        allowed_tools=["registered_tool"],
    )

    assert violations == [{
        "id": "script.hallucinated_tool_call",
        "layer": "script_tool_boundary",
        "message": "脚本调用了未注册/未允许的工具：missing_tool",
        "expected": "只能调用 selected_tools/allowed_tools 中的工具；缺工具时返回 creation_blocker，不能编造工具。",
    }]
