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
