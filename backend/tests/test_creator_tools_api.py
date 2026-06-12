from fastapi.testclient import TestClient

from backend.main import app


def test_creator_tools_endpoint_lists_registered_tools():
    client = TestClient(app)

    response = client.get("/api/creator/tools")

    assert response.status_code == 200
    body = response.json()
    assert body["override_persistence"] == "process_memory"
    tools = body["tools"]
    names = {tool["name"] for tool in tools}
    assert "text_generation" in names
    assert "wechat_publish" in names


def test_creator_tool_roles_endpoint_exposes_default_capabilities():
    client = TestClient(app)

    response = client.get("/api/creator/tool-roles")

    assert response.status_code == 200
    body = response.json()
    roles = {item["role"]: item for item in body["roles"]}
    assert roles["search_reader"]["required_capabilities"] == ["web_search"]
    assert roles["database_reader"]["required_capabilities"] == ["database_read"]
    assert "reference" not in roles
    assert "reference" in body["resource_roles"]


def test_creator_tool_patch_updates_creator_flags():
    client = TestClient(app)

    response = client.patch("/api/creator/tools/web_search", json={"enabled": False, "allow_creator_use": False})

    assert response.status_code == 200
    tool = response.json()["tool"]
    assert tool["enabled"] is False
    assert tool["allow_creator_use"] is False
    client.patch("/api/creator/tools/web_search", json={"enabled": True, "allow_creator_use": True})


def test_creator_tool_test_is_dry_run_and_does_not_echo_payload_values():
    client = TestClient(app)

    response = client.post(
        "/api/creator/tools/wechat_publish/test",
        json={"payload": {"secret": "do-not-leak", "draft_id": "abc"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["side_effect_performed"] is False
    assert body["payload_keys"] == ["draft_id", "secret"]
    assert "do-not-leak" not in str(body)


def test_creator_tool_test_passes_when_runtime_helper_is_reexported(monkeypatch):
    monkeypatch.setattr("backend.services.creator_tool_registry._dependency_available", lambda dependency: True)
    client = TestClient(app)

    response = client.post("/api/creator/tools/pdf_generation/test", json={"payload": {"title": "Demo"}})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["tool"]["configured"] is True
    assert set(body["tool"]["runtime_helpers_available"]) >= {"create_pdf", "build_pdf_report", "images_to_pdf", "merge_pdfs"}
    assert body["tool"]["missing_runtime_helpers"] == []
    assert "runtime helpers look ready" in body["message"]


def test_creator_tool_test_fails_when_tool_is_disabled_for_creator():
    client = TestClient(app)
    client.patch("/api/creator/tools/docx_parsing", json={"enabled": False, "allow_creator_use": False})
    try:
        response = client.post("/api/creator/tools/docx_parsing/test", json={"payload": {"path": "demo.docx"}})
    finally:
        client.patch("/api/creator/tools/docx_parsing", json={"enabled": True, "allow_creator_use": True})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["tool"]["configured"] is True
    assert body["tool"]["missing_runtime_helpers"] == []
    assert "disabled for Creator use" in body["message"]


def test_analyze_blueprint_reports_disabled_required_tools():
    client = TestClient(app)
    client.patch("/api/creator/tools/docx_parsing", json={"enabled": False, "allow_creator_use": False})
    try:
        response = client.post(
            "/api/creator/analyze-blueprint",
            json={
                "messages": [
                    {
                        "role": "assistant",
                        "content": "📋 Skill 架构蓝图\n- **Skill 名称**: docx-parse-demo\n- scripts/: `scripts/read_docx.py`\n  scripts/read_docx.py\n  role: docx_parser\n  inputs: path\n  outputs: text\n  required_capabilities: docx_parsing",
                    }
                ]
            },
        )
    finally:
        client.patch("/api/creator/tools/docx_parsing", json={"enabled": True, "allow_creator_use": True})

    assert response.status_code == 200
    body = response.json()
    missing = {tool["name"]: tool for tool in body["missing_tool_configs"]}
    assert "docx_parsing" in missing
    assert missing["docx_parsing"]["enabled"] is False
    assert any("docx_parsing" in warning and "禁用" in warning for warning in body["warnings"])


def test_tool_registration_flow_creates_function_card_and_registered_tool(tmp_path, monkeypatch):
    from backend.services import creator_tool_registry as registry

    monkeypatch.setattr(registry, "CUSTOM_TOOL_REGISTRY_PATH", tmp_path / "tool_registry.custom.json")
    client = TestClient(app)

    draft_response = client.post(
        "/api/creator/tools/draft",
        json={
            "tool_name": "echo_payload_tool",
            "description": "Echo payload keys for validation.",
            "tool_type": "python_helper",
            "input_description": "payload object",
            "output_description": "result object",
            "allowed_roles": ["generic_script"],
        },
    )
    assert draft_response.status_code == 200
    manifest = draft_response.json()["manifest"]
    manifest["adapter_path"] = str(tmp_path / "echo_payload_tool.py")
    assert manifest["functions"][0]["return_contract"]

    code_response = client.post("/api/creator/tools/generate-code", json={"manifest": manifest})
    assert code_response.status_code == 200
    adapter_code = code_response.json()["adapter_code"]

    validate_response = client.post(
        "/api/creator/tools/validate",
        json={"manifest": manifest, "adapter_code": adapter_code, "sample_input": {"payload": {"query": "demo"}}},
    )
    assert validate_response.status_code == 200
    validation = validate_response.json()
    assert validation["success"] is True
    assert "Tool: echo_payload_tool.echo_payload_tool" in validation["tool_card_preview"][0]
    assert "Input schema:" in validation["tool_card_preview"][0]

    register_response = client.post(
        "/api/creator/tools/register",
        json={
            "manifest": manifest,
            "adapter_code": adapter_code,
            "sample_input": {"payload": {"query": "demo"}},
            "enable": True,
            "created_by": "pytest",
        },
    )
    assert register_response.status_code == 200
    tool = register_response.json()["tool"]
    assert tool["name"] == "echo_payload_tool"
    assert tool["enabled"] is True
    assert tool["functions"][0]["function_name"] == "echo_payload_tool"
    assert registry.CUSTOM_TOOL_REGISTRY_PATH.exists()

    registry.clear_registered_tool_capabilities()
