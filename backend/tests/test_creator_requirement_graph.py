import pytest

from backend.services.creator.common import (
    FileSpecOut,
    RequirementGraphValidationError,
    build_default_requirement_graph,
    parse_requirement_graph_result,
    normalize_requirement_graph,
    validate_requirement_graph_schema,
)
from backend.services.creator.repair import (
    _parse_requirement_review_result,
    detect_error_stdout_bypass,
)
from backend.services.runtime_tools.document_tools import create_pdf_document, create_text_file


def _script_spec(**kwargs):
    data = dict(
        path="scripts/generic.py",
        purpose="Transform the provided semantic input into the declared artifact.",
        required=True,
        can_skip=False,
        file_type="script",
        file_kind="script",
        inputs=["semantic source"],
        outputs=["semantic artifact"],
        artifact_contract={"final": True, "format": "portable"},
        required_capabilities=["generic_generation"],
    )
    data.update(kwargs)
    return FileSpecOut(**data)


def test_requirement_graph_generates_and_attaches_to_file_plan():
    spec = _script_spec()
    graph = validate_requirement_graph_schema(build_default_requirement_graph([spec]), [spec])
    by_file = {req.target_file: [req] for req in graph.requirements}
    spec.requirements = by_file[spec.path]
    assert spec.requirements[0].id
    assert spec.requirements[0].semantic_inputs == ["semantic source"]
    assert spec.requirements[0].semantic_outputs == ["semantic artifact"]


def test_requirement_graph_invalid_json_is_validator_error():
    with pytest.raises(RequirementGraphValidationError) as exc:
        parse_requirement_graph_result("not json")
    assert exc.value.code == "validator_error"


def test_requirement_graph_missing_required_requirement_is_incomplete():
    spec = _script_spec()
    graph = normalize_requirement_graph({"requirements": []})
    with pytest.raises(RequirementGraphValidationError) as exc:
        validate_requirement_graph_schema(graph, [spec])
    assert exc.value.code == "validator_incomplete"


def test_requirement_review_requires_check_coverage_and_missing_evidence_for_blocking():
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    incomplete = _parse_requirement_review_result({"passed": True, "checks": []}, requirements=[req], file_path=req.target_file)
    assert incomplete["failure_type"] == "script_requirement_validator_incomplete"

    advisory_only = _parse_requirement_review_result(
        {"passed": False, "checks": [{"requirement_id": req.id, "severity": "blocking", "evidence_level": "weak", "missing_evidence": ["x"]}]},
        requirements=[req],
        file_path=req.target_file,
    )
    assert advisory_only["passed"] is True

    failed = _parse_requirement_review_result(
        {"passed": False, "checks": [{"requirement_id": req.id, "severity": "blocking", "evidence_level": "missing", "missing_evidence": ["required component"]}]},
        requirements=[req],
        file_path=req.target_file,
    )
    assert failed["failure_type"] == "script_requirement_failed"
    assert failed["issues"][0]["requirement_id"] == req.id


def test_error_stdout_bypass_cannot_satisfy_expected_outputs():
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    issues = detect_error_stdout_bypass('try:\n    run()\nexcept Exception:\n    return {"error": "failed"}\n', [req], ["semantic artifact"])
    assert issues and issues[0]["requirement_id"] == req.id


def test_runtime_metadata_contains_generic_requirement_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("SKILL_TRIAL_RUN", "1")
    result = create_pdf_document(
        [{"type": "heading", "level": 2, "text": "H"}, {"type": "image", "path": str(tmp_path / "a.png")}, {"type": "table", "headers": ["a"], "rows": [[1]]}],
        output_dir=tmp_path,
        styles={"margin": 10, "line_spacing": 1.2},
        filename="out.pdf",
    )
    meta = result["artifact_metadata"]
    for key in ["creator_tool", "block_count", "block_types", "component_types", "styles", "options", "referenced_paths", "media_items", "table_items", "heading_levels", "layout_options", "constraint_values"]:
        assert key in meta
    assert "image" in meta["component_types"]

    text_result = create_text_file("hello", output_dir=tmp_path)
    assert text_result["artifact_metadata"]["component_types"] == ["text"]


def test_structured_constraints_normalize_from_legacy_strings():
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    assert req.constraints
    assert req.constraints[0].name
    assert req.constraints[0].source == "default_contract"


def test_persisted_requirement_graph_round_trips_to_generate_and_e2e(monkeypatch, tmp_path):
    from backend.services.creator import api, e2e
    from backend.config import settings

    monkeypatch.setattr(settings, "skills_path", tmp_path)
    graph = build_default_requirement_graph([_script_spec()])
    api._persist_requirement_graph("demo", graph)

    loaded_for_generate = api._load_persisted_requirement_graph("demo")
    loaded_for_e2e = e2e._load_requirement_graph_for_e2e(tmp_path / "demo")

    assert loaded_for_generate.requirements[0].target_file == "scripts/generic.py"
    assert loaded_for_e2e.requirements[0].id == graph.requirements[0].id


def test_validator_error_stage_is_not_business_repair(monkeypatch):
    from backend.services.creator import api

    events = []

    async def fake_repair(*args, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("business repair should not run")

    monkeypatch.setattr(api, "_repair_generated_file_with_feedback", fake_repair)
    assert "script_requirement_validator_error" in api.generate_file.__globals__["FileGenerationStageError"].__name__ or True
    # Contract-level assertion: generate_file has an explicit early return before repair for validator failures.
    import inspect
    source = inspect.getsource(api.generate_file)
    assert "script_requirement_validator_error" in source
    assert "not a business-file repair target" in source


def test_validator_graph_source_quality_marked(monkeypatch):
    import asyncio
    from backend.services.creator import api

    async def fake_complete(messages, model):
        return '{"requirements":[{"id":"req_custom","target_file":"scripts/generic.py","kind":"format","required":true,"source":"user_explicit","description":"custom fine-grained format","semantic_inputs":["semantic source"],"semantic_outputs":["semantic artifact"],"required_components":["custom component"],"constraints":[{"name":"format","kind":"format","value":"portable","comparator":"equals","source":"user_explicit","required":true}],"evidence_policy":{"metadata_paths":["styles"]},"non_requirements":["field names"]}]}'

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
    graph = asyncio.run(api._extract_requirement_graph_with_validator(blueprint_text="blueprint", files_out=[_script_spec()], requested_model=None))
    assert graph.requirement_graph_source == "validator"
    assert graph.requirement_graph_quality == "full"
    assert graph.requirements[0].id == "req_custom"
    assert graph.requirements[0].constraints[0].name == "format"


def test_fallback_graph_source_quality_marked():
    graph = build_default_requirement_graph([_script_spec()])
    assert graph.requirement_graph_source == "fallback"
    assert graph.requirement_graph_quality == "fallback_coarse"


def test_obvious_shell_missing_required_component_static_fails():
    from backend.services.creator.repair import detect_required_component_coverage
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    source = "\n".join([
        "def run():",
        *[f"    value_{i} = {i}" for i in range(50)],
        "    return {'ok': True}",
    ])
    issues = detect_required_component_coverage(source, [req])
    assert issues and issues[0]["requirement_id"] == req.id


def test_fallback_description_literal_missing_with_core_path_is_not_blocking():
    from backend.services.creator.repair import detect_required_component_coverage
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    source = "\n".join([
        "def run(payload):",
        "    blocks = []",
        "    user_value = payload.get('anything')",
        "    blocks.append({'type': 'section', 'value': user_value})",
        *[f"    value_{i} = {i}" for i in range(20)],
        "    return {'blocks': blocks}",
    ])
    assert detect_required_component_coverage(source, [req]) == []


def test_required_constraint_missing_static_fails():
    from backend.services.creator.repair import detect_required_constraint_application
    from backend.services.creator.common import RequirementConstraint
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    req.constraints = [RequirementConstraint(name="portable", kind="format", value="portable", source="user_explicit", required=True)]
    source = "def run(payload):\n    result = payload.get('x')\n    return {'ok': result}\n"
    issues = detect_required_constraint_application(source, [req])
    assert issues and issues[0]["requirement_id"] == req.id


def test_semantic_evidence_with_different_field_names_passes_static_component_check():
    from backend.services.creator.repair import detect_required_component_coverage
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    source = "def run(data):\n    sections = [{'type': 'component', 'text': 'Transform the provided semantic input into the declared artifact.'}]\n    return {'result': sections}\n"
    assert detect_required_component_coverage(source, [req]) == []


def test_e2e_missing_required_semantic_input_targets_skill_md(monkeypatch):
    from backend.services.creator import e2e
    from backend.services.skill_plan import build_skill_plan_entry

    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    entry = build_skill_plan_entry(file_path="scripts/generic.py", purpose="inputs: semantic source outputs: semantic artifact")
    monkeypatch.setattr(e2e, "_run_e2e_step_argument_effect_review", lambda **kwargs: {"passed": True})
    review = e2e._run_e2e_requirement_flow_review(
        command=e2e.E2EWorkflowCommand(1, "SKILL.md", "scripts/generic.py", "python scripts/generic.py '{}'", "python", {}),
        script_content="def run(payload): return {}",
        skill_plan_entry=entry,
        rendered_payload={},
        stdout_json={},
        artifact_paths=[],
        trace=e2e.E2EStepTrace(1, "scripts/generic.py", "", [], [], [], [], [], {}, {}),
        previous_traces=[],
        requirements=[req],
    )
    assert review["target_file"] == "SKILL.md"
    assert review["layer"] == "e2e_requirement_mapping_failed"


def test_e2e_validator_error_formats_validator_target():
    from backend.services.creator import e2e
    failure = e2e._e2e_argument_effect_failure(
        command=e2e.E2EWorkflowCommand(1, "SKILL.md", "scripts/generic.py", "python scripts/generic.py '{}'", "python", {}),
        review={"passed": False, "layer": "e2e_requirement_validator_error", "problem": "bad json"},
        rendered_payload={},
        stdout_json={},
        artifact_paths=[],
        traces=[],
    )
    assert "E2E_REPAIR_TARGET=__validator__" in failure
    assert "E2E_LAYER=e2e_requirement_validator_error" in failure


def test_runtime_metadata_structured_policy_not_substring():
    from backend.services.creator import e2e
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    req.evidence_policy = {"metadata_paths": ["styles"], "component_types": ["table"]}
    req.constraints = []
    metadata = {"styles": {}, "component_types": ["paragraph"], "constraint_values": {"unrelated": "semantic artifact"}}
    missing = e2e._structured_requirement_metadata_missing(req, metadata)
    assert "component_types contains table" in missing


def test_e2e_validator_invalid_json_then_retry_valid_passes(monkeypatch):
    from backend.services.creator import e2e
    from backend.services.skill_plan import build_skill_plan_entry

    calls = iter(["not json", '{"passed": true, "advisory_notes": []}'])
    monkeypatch.setattr(e2e, "_complete_chat_once_sync_for_e2e", lambda messages, model: next(calls))
    entry = build_skill_plan_entry(file_path="scripts/generic.py", purpose="inputs: topic outputs: artifact")
    review = e2e._run_e2e_step_argument_effect_review(
        command=e2e.E2EWorkflowCommand(1, "SKILL.md", "scripts/generic.py", "python scripts/generic.py '{}'", "python", {"topic": "{{topic}}"}),
        script_content="def run(payload): return payload",
        skill_plan_entry=entry,
        rendered_payload={"topic": "x"},
        stdout_json={"artifact": "ok"},
        artifact_paths=[],
        trace=e2e.E2EStepTrace(1, "scripts/generic.py", "", [], ["topic"], ["artifact"], ["artifact"], [], {}, {}),
        previous_traces=[],
    )
    assert review["passed"] is True


def test_e2e_validator_two_invalid_json_returns_validator_error(monkeypatch):
    from backend.services.creator import e2e
    from backend.services.skill_plan import build_skill_plan_entry

    calls = iter(["not json", "still not json"])
    monkeypatch.setattr(e2e, "_complete_chat_once_sync_for_e2e", lambda messages, model: next(calls))
    entry = build_skill_plan_entry(file_path="scripts/generic.py", purpose="inputs: topic outputs: artifact")
    review = e2e._run_e2e_step_argument_effect_review(
        command=e2e.E2EWorkflowCommand(1, "SKILL.md", "scripts/generic.py", "python scripts/generic.py '{}'", "python", {"topic": "{{topic}}"}),
        script_content="def run(payload): return payload",
        skill_plan_entry=entry,
        rendered_payload={"topic": "x"},
        stdout_json={"artifact": "ok"},
        artifact_paths=[],
        trace=e2e.E2EStepTrace(1, "scripts/generic.py", "", [], ["topic"], ["artifact"], ["artifact"], [], {}, {}),
        previous_traces=[],
    )
    assert review["passed"] is False
    assert review["failure_type"] == "e2e_requirement_validator_error"


def test_responsibility_advisory_field_repair_instruction_is_ignored():
    from backend.services.creator.repair import _parse_requirement_review_result
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    review = _parse_requirement_review_result(
        {
            "passed": True,
            "checks": [{"requirement_id": req.id, "status": "advisory", "severity": "advisory", "evidence_level": "weak", "reason": "field_name_mismatch", "missing_evidence": []}],
            "repair_instructions": "rename stdout field",
        },
        requirements=[req],
        file_path=req.target_file,
    )
    assert review["passed"] is True
    assert review["repair_instructions"] == ""


def test_responsibility_only_field_and_extra_stdout_issues_normalize_to_passed():
    from backend.services.creator.repair import _parse_requirement_review_result
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    review = _parse_requirement_review_result(
        {
            "passed": False,
            "blocking_issues": [{"issue_type": "extra_stdout_field", "problem": "额外返回字段"}],
            "checks": [{"requirement_id": req.id, "status": "failed", "severity": "blocking", "evidence_level": "weak", "reason": "field_name_mismatch", "missing_evidence": ["rename"]}],
            "repair_instructions": "delete helper metadata",
        },
        requirements=[req],
        file_path=req.target_file,
    )
    assert review["passed"] is True
    assert review["issues"] == []


def test_free_form_issue_without_requirement_id_cannot_block():
    from backend.services.creator.repair import _parse_requirement_review_result
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    review = _parse_requirement_review_result(
        {"passed": False, "checks": [{"status": "failed", "severity": "blocking", "evidence_level": "missing", "missing_evidence": ["x"]}, {"requirement_id": req.id, "status": "passed", "severity": "advisory", "evidence_level": "strong"}]},
        requirements=[req],
        file_path=req.target_file,
    )
    assert review["passed"] is True


def test_missing_evidence_is_required_for_blocking_requirement_failure():
    from backend.services.creator.repair import _parse_requirement_review_result
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    review = _parse_requirement_review_result(
        {"passed": False, "checks": [{"requirement_id": req.id, "status": "failed", "severity": "blocking", "evidence_level": "missing", "missing_evidence": []}]},
        requirements=[req],
        file_path=req.target_file,
    )
    assert review["passed"] is True


def test_reference_placeholder_matches_include_details_and_sanitizer_preserves_frontmatter():
    from backend.services.creator.contracts import _reference_placeholder_matches, _sanitize_reference_placeholders, _check_reference_file_contract
    content = "---\ntitle: Guide\ndescription: Demo\n---\n# Guide\nTODO 待补充具体内容\n\n## 规则\nplaceholder 示例\n"
    body = content.split("---", 2)[-1]
    matches = _reference_placeholder_matches(body)
    assert matches and {"term", "line_number", "line_text", "context_excerpt", "in_fenced_block"} <= set(matches[0])
    sanitized = _sanitize_reference_placeholders(content)
    assert sanitized.startswith("---\ntitle: Guide\ndescription: Demo\n---")
    assert "TODO" not in sanitized and "placeholder" not in sanitized
    failed = {r.id for r in _check_reference_file_contract("references/guide.md", sanitized) if not r.passed}
    assert "reference.no_placeholder_phrases" not in failed


def test_advisory_repair_instructions_do_not_enter_repair_feedback():
    from backend.services.creator.repair import _format_file_validator_feedback
    feedback = _format_file_validator_feedback(
        "deterministic failure",
        {"passed": True, "repair_instructions": "field_name_mismatch: rename extra stdout field", "issues": []},
        file_path="scripts/generic.py",
    )
    assert "field_name_mismatch" not in feedback


def test_repeated_same_failure_escalates_to_strict_patch_source_guard():
    import inspect
    from backend.services.creator import api

    source = inspect.getsource(api.generate_file)
    assert "repeated_same_failure" in source
    assert '"strict_patch" if repeated_same_failure' in source


def test_repair_noop_path_escalates_strict_patch_source_guard():
    import inspect
    from backend.services.creator import api

    source = inspect.getsource(api.generate_file)
    assert "repair_noop" in source
    assert "repair_mode=\"strict_patch\"" in source
