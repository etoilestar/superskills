import pytest

from backend.services.platform_io_contract import (
    build_platform_io_contract,
    platform_io_contract_prompt_text,
    platform_output_names,
    project_function_item_outputs_to_platform_contract,
)
from backend.services.creator.common import RequirementGraph, build_default_requirement_graph, normalize_requirement_graph
from backend.services.creator.generation import _build_script_generate_file_prompt_variant, _build_generate_file_prompt
from backend.services.creator.repair import CreatorDiffProposal, CreatorRepairScope, _validate_repair_diff_scope


class DummyFile:
    path = "scripts/report.py"
    purpose = "create report pdf"
    inputs = ["text"]
    outputs = ["pdf_path"]
    required_capabilities = ["pdf_generation"]
    selected_tools = []
    dependencies = []
    runtime_contract = {}
    artifact_contract = {"pdf_path": "pdf"}
    role = "pdf_builder"
    file_kind = "script"
    runtime = "python"
    entrypoint = "scripts/report.py"


def _prompt_text(messages):
    return "\n".join(str(m.get("content") or "") for m in messages)


def test_platform_io_contract_declares_output_dir_final_outputs_dir():
    contract = build_platform_io_contract()
    prompt = platform_io_contract_prompt_text()
    assert "final outputs directory" in contract["environment"]["OUTPUT_DIR"]
    assert "OUTPUT_DIR already points to final outputs directory" in prompt
    assert "Do not append 'outputs' to OUTPUT_DIR" in prompt


def test_typed_runtime_output_is_projected_without_name_special_case():
    base = build_platform_io_contract()
    projected = project_function_item_outputs_to_platform_contract(base, [{
        "outputs": [{"name": "archive_path", "type": "file"}],
    }])
    assert "archive_path" in platform_output_names(projected)
    assert "archive_path" not in platform_output_names(base)


def test_intermediate_or_untyped_output_cannot_expand_platform_boundary():
    projected = project_function_item_outputs_to_platform_contract(
        build_platform_io_contract(),
        [{"outputs": [
            {"name": "private_path", "type": "file", "role": "intermediate_output"},
            {"name": "arbitrary_stdout_key"},
        ]}],
    )
    assert "private_path" not in platform_output_names(projected)
    assert "arbitrary_stdout_key" not in platform_output_names(projected)


def test_platform_input_source_families_are_optional_and_have_no_structured_wrapper():
    boundary = build_platform_io_contract()["platform_skill_boundary"]
    assert boundary["input_envelope_fields"] == [
        "user_request", "input", "text", "payload", "options",
        "input_files", "files", "resources",
    ]
    semantics = boundary["input_source_semantics"]
    assert semantics["freeform_request"] == {
        "canonical": "user_request",
        "representations": ["user_request", "input", "text"],
        "globally_required": False,
    }
    assert semantics["runtime_files"]["canonical"] == "input_files"
    assert semantics["runtime_files"]["representations"] == ["input_files", "files"]
    assert "structured_parameters" not in semantics
    assert "preferred_structured_input_root" not in boundary
    assert all(not family["globally_required"] for family in semantics.values())

    prompt = platform_io_contract_prompt_text()
    assert "PLATFORM INPUT SEMANTICS" in prompt
    assert "No platform input source is globally required" in prompt
    assert "DESCRIPTIVE, NOT A CLOSED WHITELIST" in prompt
    assert "there is no default structured-parameter wrapper" in prompt


def test_requirement_graph_injects_and_normalize_overrides_platform_io_contract():
    graph = build_default_requirement_graph([DummyFile()])
    assert graph.platform_io_contract["environment"]["OUTPUT_DIR"].startswith("already points")

    forged = {
        "platform_io_contract": {"environment": {"OUTPUT_DIR": "forged"}},
        "requirements": [
            {"target_file": "scripts/report.py", "purpose": "make report", "inputs": [], "outputs": []}
        ],
    }
    normalized = normalize_requirement_graph(forged)
    assert normalized.platform_io_contract != forged["platform_io_contract"]
    assert "final outputs directory" in normalized.platform_io_contract["environment"]["OUTPUT_DIR"]

    forged_model = RequirementGraph(platform_io_contract={"bad": True})
    normalized_model = normalize_requirement_graph(forged_model)
    assert "bad" not in normalized_model.platform_io_contract


def test_script_generation_prompt_contains_platform_io_bans_and_helper_filename_rule():
    text = _prompt_text(_build_script_generate_file_prompt_variant(
        file_path="scripts/report.py",
        skill_name="demo",
        purpose="create pdf report",
        blueprint_text="Create a PDF report.",
        role="pdf_builder",
        skill_plan_entry={"path": "scripts/report.py", "role": "pdf_builder", "runtime": "python", "required_capabilities": ["pdf_generation"]},
        variant="standard",
    ))
    assert "禁止 OUTPUT_DIR/outputs" in text
    assert 'replace("/tmp/", "outputs/")' in text
    assert 'filename="report.pdf"' in text
    assert "filename=full_path" in text


def test_script_generation_prompt_declares_runtime_binding_authority_and_ports():
    text = _prompt_text(_build_script_generate_file_prompt_variant(
        file_path="scripts/report.py",
        skill_name="demo",
        purpose="create report",
        blueprint_text="",
        role="generic_script",
        skill_plan_entry={
            "path": "scripts/report.py",
            "role": "generic_script",
            "runtime": "python",
            "inputs": ["input_files", "fields"],
            "input_binding": [
                {"argv_key": "input_files", "value_type": "file_list"},
                {"argv_key": "fields", "value_type": "object"},
            ],
        },
        variant="standard",
    ))
    assert "## Runtime Binding Authority" in text
    assert "not designing the caller protocol" in text
    assert "wrap existing inputs into another object" in text
    assert '"name": "input_files"' in text
    assert '"type": "file_list"' in text
    assert '"binding_status": "resolved"' in text


def test_skill_md_prompt_requires_role_inputs_outputs_action_schema():
    text = _prompt_text(_build_generate_file_prompt(
        file_path="SKILL.md",
        skill_name="demo",
        purpose="skill docs",
        blueprint_text="Files: scripts/report.py",
        conversation_history=[],
        role=None,
        skill_plan_entry=None,
    ))
    assert "role: ..." in text
    assert "inputs: ..." in text
    assert "outputs: ..." in text
    assert "command JSON argv keys" in text


def test_skill_md_prompt_preserves_frozen_binding_authority():
    text = _prompt_text(_build_generate_file_prompt(
        file_path="SKILL.md",
        skill_name="demo",
        purpose="skill docs",
        blueprint_text="Files: scripts/report.py",
        conversation_history=[],
        role=None,
        skill_plan_entry=None,
    ))
    assert "FIRST-ROUND BINDING AUTHORITY" in text
    assert "available_sources may be considered only for unresolved_target_keys" in text
    assert "Frozen Interface / Graph > command_alignment_snapshot" in text
    assert "The execution environment already provides runtime inputs" in text
    assert "It should not describe a new invocation protocol" in text
    assert "custom JSON request bodies" in text


@pytest.mark.parametrize("bad_new", [
    'output_dir = os.path.join(output_dir, "outputs")\n',
    'path = path.replace("/tmp/", "outputs/")\n',
])
def test_repair_patch_rejects_new_platform_io_violations(bad_new):
    original = "def run():\n    return {}\n"
    proposal = CreatorDiffProposal(
        target_file="scripts/report.py",
        reason="bad io",
        edits=[{"old": "def run():\n    return {}\n", "new": "def run():\n    " + bad_new + "    return {}\n"}],
    )
    with pytest.raises(ValueError, match="platform_io_contract_violation"):
        _validate_repair_diff_scope(
            proposal=proposal,
            current_content=original,
            scope=CreatorRepairScope(phase="test", repair_type="localized_patch", target_file="scripts/report.py"),
        )
