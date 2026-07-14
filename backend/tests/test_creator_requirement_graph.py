import json

import pytest

from backend.services.creator.common import (
    FileSpecOut,
    FunctionItem,
    ResponsibilityGraph,
    RequirementGraphValidationError,
    build_default_requirement_graph,
    parse_requirement_graph_result,
    normalize_requirement_graph,
    validate_requirement_graph_schema,
)
from backend.services.creator.repair import (
    _detect_script_responsibility_static_blockers,
    _parse_requirement_review_result,
    _runtime_tool_contract_static_blockers,
    _run_script_responsibility_review,
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


def _file_spec(path: str, **kwargs):
    data = dict(
        path=path,
        purpose=f"Executable or supporting responsibility for {path}.",
        required=True,
        can_skip=False,
        file_type="script" if path.startswith("scripts/") else "resource",
        file_kind="script" if path.startswith("scripts/") else "reference",
        inputs=["source input"],
        outputs=["declared output"],
        constraints=[{"name": f"constraint_{path.replace('/', '_')}", "kind": "constraint", "value": "preserve", "comparator": "describes"}],
        required_capabilities=["declared_capability"],
    )
    if path.startswith("assets/"):
        data["file_kind"] = "asset"
    if path == "SKILL.md":
        data["file_kind"] = "skill_overview"
    data.update(kwargs)
    return FileSpecOut(**data)


def test_responsibility_graph_contains_script_function_items_only():
    specs = [
        _file_spec("SKILL.md"),
        _file_spec("scripts/a.py"),
        _file_spec("scripts/b.py"),
        _file_spec("references/guide.md"),
        _file_spec("assets/template.bin"),
    ]

    graph = build_default_requirement_graph(specs)

    assert {item.target_file for item in graph.function_items} == {
        "scripts/a.py",
        "scripts/b.py",
    }


def test_reference_with_substantive_contract_never_becomes_function_item():
    graph = build_default_requirement_graph([
        _file_spec(
            "references/guide.md",
            purpose="Substantive supporting guide with declared output.",
            inputs=["brief"],
            outputs=["guide"],
            constraints=[{"name": "guide_rule", "kind": "constraint", "value": "follow"}],
            required_capabilities=["reference_authoring"],
        ),
        _file_spec("scripts/a.py"),
    ])

    assert [item.target_file for item in graph.function_items] == ["scripts/a.py"]


def test_one_script_maps_to_exactly_one_function_item():
    graph = validate_requirement_graph_schema(
        build_default_requirement_graph([_file_spec("scripts/a.py")]),
        [_file_spec("scripts/a.py")],
    )

    assert len(graph.function_items) == 1
    assert graph.function_items[0].target_file == "scripts/a.py"


def test_duplicate_function_item_target_is_validator_incomplete():
    graph = ResponsibilityGraph(requirements=[
        FunctionItem(target_file="scripts/a.py", purpose="first"),
        FunctionItem(target_file="scripts/a.py", purpose="second"),
    ])

    with pytest.raises(RequirementGraphValidationError) as exc:
        validate_requirement_graph_schema(graph, [_file_spec("scripts/a.py")])

    assert exc.value.code == "validator_incomplete"


def test_non_script_function_item_is_validator_incomplete():
    graph = ResponsibilityGraph(requirements=[
        FunctionItem(target_file="references/guide.md", purpose="cannot execute"),
    ])

    with pytest.raises(RequirementGraphValidationError) as exc:
        validate_requirement_graph_schema(graph, [_file_spec("references/guide.md")])

    assert exc.value.code == "validator_incomplete"


def test_unknown_script_function_item_is_validator_incomplete():
    graph = ResponsibilityGraph(requirements=[
        FunctionItem(target_file="scripts/a.py", purpose="known script responsibility"),
        FunctionItem(target_file="scripts/ghost.py", purpose="unknown script responsibility"),
    ])

    with pytest.raises(RequirementGraphValidationError) as exc:
        validate_requirement_graph_schema(graph, [_file_spec("scripts/a.py")])

    assert exc.value.code == "validator_incomplete"


def test_script_constraints_project_to_function_item_but_reference_constraints_do_not():
    graph = build_default_requirement_graph([
        _file_spec("scripts/a.py", constraints=[{"name": "script_rule", "kind": "constraint", "value": "apply"}]),
        _file_spec("references/guide.md", constraints=[{"name": "reference_rule", "kind": "constraint", "value": "resource"}]),
    ])

    assert [item.target_file for item in graph.function_items] == ["scripts/a.py"]
    assert [constraint.name for constraint in graph.function_items[0].constraints] == ["script_rule"]


def test_legacy_requirement_graph_wire_shape_still_round_trips():
    payload = parse_requirement_graph_result({
        "requirements": [
            {"target_file": "scripts/a.py", "purpose": "execute action", "outputs": ["result"]}
        ]
    })

    graph = normalize_requirement_graph(payload)

    assert graph.requirements[0].target_file == "scripts/a.py"
    assert graph.function_items[0].outputs == ["result"]
    assert "function_items" not in graph.model_dump(mode="json")


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
    assert incomplete["passed"] is True
    assert incomplete["failure_type"] == "script_requirement_validator_incomplete"

    advisory_only = _parse_requirement_review_result(
        {"passed": False, "checks": [{"requirement_id": req.id, "severity": "blocking", "evidence_level": "weak", "missing_evidence": ["x"]}]},
        requirements=[req],
        file_path=req.target_file,
    )
    assert advisory_only["passed"] is True

    failed = _parse_requirement_review_result(
        {"passed": False, "checks": [{"requirement_id": req.id, "scope": "current_file_only", "failure_layer": "responsibility", "semantic_failure": "core transformation is absent", "severity": "blocking", "evidence_level": "missing", "missing_evidence": ["required component"]}]},
        requirements=[req],
        file_path=req.target_file,
    )
    assert failed["failure_type"] == "script_requirement_failed"
    assert failed["issues"][0]["requirement_id"] == req.id




def test_warning_with_required_missing_evidence_is_backend_blocking():
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    review = _parse_requirement_review_result(
        {
            "passed": True,
            "checks": [{
                "requirement_id": req.id,
                "scope": "current_file_only",
                "failure_layer": "responsibility",
                "semantic_failure": "core product construction is absent",
                "severity": "warning",
                "evidence_level": "missing",
                "missing_evidence": ["core product construction"],
                "allowed_scope": "advisory text should not decide blocking",
            }],
        },
        requirements=[req],
        file_path=req.target_file,
    )
    assert review["passed"] is False
    assert review["failure_type"] == "script_requirement_failed"


def test_advisory_collection_with_required_missing_evidence_is_backend_blocking():
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    review = _parse_requirement_review_result(
        {
            "passed": True,
            "checks": [{"requirement_id": req.id, "evidence_level": "present", "missing_evidence": []}],
            "advisory_notes": [{
                "requirement_id": req.id,
                "scope": "current_file_only",
                "failure_layer": "responsibility",
                "semantic_failure": "helper result never participates in output",
                "evidence_level": "missing",
                "missing_evidence": ["helper result output"],
            }],
        },
        requirements=[req],
        file_path=req.target_file,
    )
    assert review["passed"] is False
    assert review["issues"][0]["allowed_scope"] == "current file only"

def test_static_responsibility_blocks_required_input_not_in_core_path():
    spec = _script_spec(inputs=["customer brief"], outputs=["report path"])
    req = build_default_requirement_graph([spec]).requirements[0]
    script = "def run(payload):\n    blocks = [{'type': 'text', 'text': 'fixed'}]\n    return {'path': 'out.pdf'}\n"
    issues = _detect_script_responsibility_static_blockers(script, spec, [req])
    assert issues
    assert issues[0]["id"] == "semantic_responsibility_missing"
    assert issues[0]["allowed_scope"] == "current script only"




def test_static_responsibility_blocks_generic_shell_without_semantic_product():
    spec = _script_spec(inputs=["customer brief"], outputs=["report path"])
    req = build_default_requirement_graph([spec]).requirements[0]
    script = """
def run(payload):
    source = payload.get('any_alias')
    return {'status': 'ok', 'message': 'done'}
"""
    issues = _detect_script_responsibility_static_blockers(script, spec, [req])
    assert issues
    assert issues[0]["id"] == "semantic_responsibility_missing"
    assert "字段" not in issues[0]["minimal_edit"] or "固定字段名" in issues[0]["minimal_edit"]


def test_static_responsibility_passes_when_input_enters_blocks_and_helper_args():
    spec = _script_spec(inputs=["customer brief"], outputs=["report path"])
    req = build_default_requirement_graph([spec]).requirements[0]
    script = """
def run(payload):
    brief = payload.get('customer brief')
    sections = [{'type': 'text', 'text': brief}]
    result = create_pdf_document(sections, filename='out.pdf')
    print({'path': result.get('path'), 'debug': {'block_count': len(sections)}})
"""
    assert _detect_script_responsibility_static_blockers(script, spec, [req]) == []


def test_static_responsibility_allows_different_field_name_when_input_flows():
    spec = _script_spec(inputs=["customer brief"], outputs=["report path"])
    req = build_default_requirement_graph([spec]).requirements[0]
    script = """
def run(payload):
    source = payload.get('brief_text')
    blocks = [{'type': 'text', 'text': source}]
    result = create_pdf_document(blocks, filename='out.pdf')
    return {'artifact': result, 'metadata': {'source': 'payload'}}
"""
    assert _detect_script_responsibility_static_blockers(script, spec, [req]) == []


def test_static_responsibility_allows_different_intermediate_representation():
    spec = _script_spec(inputs=["customer brief"], outputs=["report path"])
    req = build_default_requirement_graph([spec]).requirements[0]
    script = """
def run(payload):
    source = payload.get('brief_text')
    rows = [{'kind': 'paragraph', 'value': source}]
    assembled = {'rows': rows, 'options': {'format': 'portable'}}
    return {'artifact': assembled, 'extra_stdout_metadata': {'ok': True}}
"""
    assert _detect_script_responsibility_static_blockers(script, spec, [req]) == []


def test_runtime_tool_contract_blocks_unknown_helper():
    spec = _script_spec(
        selected_tools=["text_file"],
        required_capabilities=["text_file"],
    )
    req = build_default_requirement_graph([spec]).requirements[0]
    script = """
from backend.services.runtime_tools import missing_runtime_helper

def run(payload):
    result = missing_runtime_helper({'text': payload.get('brief_text')})
    return {'artifact': result}
"""
    issues = _runtime_tool_contract_static_blockers(script, spec, [req])
    assert issues
    assert issues[0]["id"] == "tool_contract_mismatch"


def test_runtime_tool_contract_allows_known_helper_when_allowlist_absent():
    spec = _script_spec(selected_tools=[], runtime_contract={})
    req = build_default_requirement_graph([spec]).requirements[0]
    script = """
from backend.services.runtime_tools import create_text_file

def run(payload):
    result = create_text_file(text=str(payload.get('brief_text') or ''), filename='out.txt')
    return {'artifact': result, 'extra_stdout_metadata': {'ok': True}}
"""
    assert _runtime_tool_contract_static_blockers(script, spec, [req]) == []


def test_runtime_tool_contract_blocks_known_helper_when_explicitly_not_allowed():
    spec = _script_spec(
        selected_tools=[],
        required_capabilities=[],
        runtime_contract={"selected_tools": ["text_generation"]},
    )
    req = build_default_requirement_graph([spec]).requirements[0]
    script = """
from backend.services.runtime_tools import create_text_file

def run(payload):
    result = create_text_file(text=str(payload.get('brief_text') or ''), filename='out.txt')
    return {'artifact': result}
"""
    issues = _runtime_tool_contract_static_blockers(script, spec, [req])
    assert issues
    assert issues[0]["id"] == "tool_contract_mismatch"


@pytest.mark.asyncio
async def test_tool_contract_mismatch_is_model_judged_and_enters_patchable_failure(monkeypatch):
    spec = _script_spec(selected_tools=[], required_capabilities=[])
    req = build_default_requirement_graph([spec]).requirements[0]

    async def fake_complete(*args, **kwargs):
        return json.dumps({
            "passed": False,
            "blocking_issues": [{
                "id": "tool_contract_mismatch",
                "severity": "error",
                "scope": "current_file_only",
                "failure_layer": "responsibility",
                "failed_file": spec.path,
                "problem": "Source calls a platform tool absent from the Current File ToolPool contracts.",
                "evidence": "from backend.services.skill_runtime import arbitrary_callable",
                "minimal_edit": "Use a tool from the current contract or request tool support.",
            }],
        })

    monkeypatch.setattr("backend.services.creator.repair.complete_chat_once", fake_complete)
    review = await _run_script_responsibility_review(
        file_path=spec.path,
        script_content="from backend.services.skill_runtime import arbitrary_callable\n\ndef run(payload):\n    return arbitrary_callable(payload)\n",
        skill_plan_entry=spec,
        requirements=[req],
    )
    assert review["passed"] is False
    assert review["issues"][0]["id"] == "tool_contract_mismatch"


@pytest.mark.asyncio
async def test_validator_field_name_mismatch_is_advisory_not_first_round_patch(monkeypatch):
    spec = _script_spec(inputs=["customer brief"], outputs=["report path"])
    req = build_default_requirement_graph([spec]).requirements[0]

    async def fake_complete(*args, **kwargs):
        return '{"passed": false, "checks": [{"requirement_id": "' + req.id + '", "evidence_level": "present", "missing_evidence": []}], "blocking_issues": [{"requirement_id": "' + req.id + '", "evidence_level": "missing", "issue_type": "field_name_mismatch", "missing_evidence": ["output key mismatch"]}]}'

    monkeypatch.setattr("backend.services.creator.repair.complete_chat_once", fake_complete)
    script = """
def run(payload):
    source = payload.get('brief_text')
    rows = [{'kind': 'paragraph', 'value': source}]
    return {'artifact': rows, 'extra_stdout_metadata': {'ok': True}}
"""
    review = await _run_script_responsibility_review(
        file_path=spec.path,
        script_content=script,
        skill_plan_entry=spec,
        requirements=[req],
    )
    assert review["passed"] is True


@pytest.mark.asyncio
async def test_tool_support_insufficient_is_model_judged_for_toolpool_augmentation(monkeypatch):
    spec = _script_spec(selected_tools=[], required_capabilities=[])
    req = build_default_requirement_graph([spec]).requirements[0]

    async def fake_complete(*args, **kwargs):
        return json.dumps({
            "passed": False,
            "blocking_issues": [{
                "id": "tool_support_insufficient",
                "severity": "error",
                "scope": "current_file_only",
                "failure_layer": "responsibility",
                "failed_file": spec.path,
                "problem": "Current ToolPool lacks the callable needed to complete this FunctionItem.",
                "evidence": "authorized_tool_contracts is empty",
                "minimal_edit": "Request first-round ToolPool augmentation instead of changing E2E.",
            }],
        })

    monkeypatch.setattr("backend.services.creator.repair.complete_chat_once", fake_complete)
    review = await _run_script_responsibility_review(
        file_path=spec.path,
        script_content="def run(payload):\n    return {'text': ''}\n",
        skill_plan_entry=spec,
        requirements=[req],
    )
    assert review["passed"] is False
    assert review["issues"][0]["id"] == "tool_support_insufficient"


@pytest.mark.asyncio
async def test_validator_passed_with_static_blocker_enters_script_requirement_failed(monkeypatch):
    spec = _script_spec(inputs=["customer brief"], outputs=["report path"])
    req = build_default_requirement_graph([spec]).requirements[0]

    async def fake_complete(*args, **kwargs):
        return '{"passed": true, "checks": [{"requirement_id": "' + req.id + '", "severity": "warning", "evidence_level": "weak", "missing_evidence": []}], "advisory_notes": [{"requirement_id": "' + req.id + '", "evidence_level": "missing", "missing_evidence": ["input not used"]}]}'

    monkeypatch.setattr("backend.services.creator.repair.complete_chat_once", fake_complete)
    review = await _run_script_responsibility_review(
        file_path=spec.path,
        script_content="def run(payload):\n    return {'path': 'fixed.pdf'}\n",
        skill_plan_entry=spec,
        requirements=[req],
    )
    assert review["failure_type"] == "script_requirement_failed"
    assert review["issues"][0]["allowed_scope"] == "current script only"


@pytest.mark.asyncio
async def test_validator_incomplete_twice_static_blocker_repairs_script(monkeypatch):
    spec = _script_spec(inputs=["customer brief"], outputs=["report path"])
    req = build_default_requirement_graph([spec]).requirements[0]

    async def fake_complete(*args, **kwargs):
        return '{"passed": true, "blocking_issues": [], "advisory_notes": []}'

    monkeypatch.setattr("backend.services.creator.repair.complete_chat_once", fake_complete)
    review = await _run_script_responsibility_review(
        file_path=spec.path,
        script_content="def run(payload):\n    blocks = []\n    return {'path': 'fixed.pdf'}\n",
        skill_plan_entry=spec,
        requirements=[req],
    )
    assert review["failure_type"] == "script_requirement_failed"


@pytest.mark.asyncio
async def test_validator_incomplete_twice_without_static_blocker_stays_incomplete(monkeypatch):
    spec = _script_spec(inputs=["customer brief"], outputs=["report path"])
    req = build_default_requirement_graph([spec]).requirements[0]

    async def fake_complete(*args, **kwargs):
        return '{"passed": true, "blocking_issues": [], "advisory_notes": []}'

    monkeypatch.setattr("backend.services.creator.repair.complete_chat_once", fake_complete)
    script = """
def run(payload):
    brief = payload.get('brief_text')
    blocks = [{'type': 'text', 'text': brief}]
    result = create_pdf_document(blocks, filename='out.pdf')
    return {'artifact': result, 'extra_stdout_metadata': {'ok': True}}
"""
    review = await _run_script_responsibility_review(
        file_path=spec.path,
        script_content=script,
        skill_plan_entry=spec,
        requirements=[req],
    )
    assert review["failure_type"] == "script_requirement_validator_incomplete"


@pytest.mark.asyncio
async def test_responsibility_review_invalid_json_is_rewritten_before_validating(monkeypatch):
    spec = _script_spec(inputs=["customer brief"], outputs=["report path"])
    req = build_default_requirement_graph([spec]).requirements[0]
    calls = iter([
        "not json",
        '{"passed": true, "blocking_issues": [], "advisory_notes": []}',
    ])

    async def fake_complete(*args, **kwargs):
        return next(calls)

    monkeypatch.setattr("backend.services.creator.repair.complete_chat_once", fake_complete)
    script = """
def run(payload):
    brief = payload.get('brief_text')
    blocks = [{'type': 'text', 'text': brief}]
    result = create_pdf_document(blocks, filename='out.pdf')
    return {'artifact': result}
"""
    review = await _run_script_responsibility_review(
        file_path=spec.path,
        script_content=script,
        skill_plan_entry=spec,
        requirements=[req],
    )
    assert review["passed"] is True


@pytest.mark.asyncio
async def test_responsibility_review_invalid_json_does_not_become_business_patch_without_static_blocker(monkeypatch):
    spec = _script_spec(inputs=["customer brief"], outputs=["report path"])
    req = build_default_requirement_graph([spec]).requirements[0]

    async def fake_complete(*args, **kwargs):
        return "still not json"

    monkeypatch.setattr("backend.services.creator.repair.complete_chat_once", fake_complete)
    script = """
def run(payload):
    brief = payload.get('brief_text')
    blocks = [{'type': 'text', 'text': brief}]
    result = create_pdf_document(blocks, filename='out.pdf')
    return {'artifact': result}
"""
    review = await _run_script_responsibility_review(
        file_path=spec.path,
        script_content=script,
        skill_plan_entry=spec,
        requirements=[req],
    )
    assert review["passed"] is False
    assert review["failure_type"] == "script_requirement_validator_incomplete"
    assert review["issues"][0]["id"] == "script_responsibility.validator_schema_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"advisory_notes": []},
    {"passed": "false", "blocking_issues": []},
    {"passed": True, "blocking_issues": "bad"},
])
async def test_responsibility_review_schema_invalid_never_passes(monkeypatch, payload):
    spec = _script_spec(inputs=["customer brief"], outputs=["report path"])
    req = build_default_requirement_graph([spec]).requirements[0]

    async def fake_complete(*args, **kwargs):
        return json.dumps(payload)

    monkeypatch.setattr("backend.services.creator.repair.complete_chat_once", fake_complete)
    review = await _run_script_responsibility_review(
        file_path=spec.path,
        script_content="def run(payload):\n    return {'artifact': payload}\n",
        skill_plan_entry=spec,
        requirements=[req],
    )
    assert review["passed"] is False
    assert review["failure_type"] == "script_requirement_validator_incomplete"
    assert review["issues"][0]["id"] == "script_responsibility.validator_schema_invalid"


@pytest.mark.asyncio
async def test_responsibility_review_three_schema_invalid_returns_validator_failure(monkeypatch):
    spec = _script_spec(inputs=["customer brief"], outputs=["report path"])
    req = build_default_requirement_graph([spec]).requirements[0]
    calls = {"count": 0}

    async def fake_complete(*args, **kwargs):
        calls["count"] += 1
        return json.dumps({"passed": True, "blocking_issues": "bad"})

    monkeypatch.setattr("backend.services.creator.repair.complete_chat_once", fake_complete)
    review = await _run_script_responsibility_review(
        file_path=spec.path,
        script_content="def run(payload):\n    return {'artifact': payload}\n",
        skill_plan_entry=spec,
        requirements=[req],
    )
    assert calls["count"] == 3
    assert review["passed"] is False
    assert review["failure_type"] == "script_requirement_validator_incomplete"


@pytest.mark.asyncio
async def test_requirement_items_use_judge_schema_without_checks_parser(monkeypatch):
    spec = _script_spec(inputs=["prompt"], outputs=["image path"])
    req = build_default_requirement_graph([spec]).requirements[0]

    async def fake_complete(*args, **kwargs):
        return json.dumps({
            "passed": False,
            "blocking_issues": [{
                "issue_type": "semantic_action_incomplete",
                "semantic_failure": "current script does not perform its core responsibility",
            }],
            "advisory_notes": [],
            "repair_instructions": "implement the current script responsibility",
        })

    monkeypatch.setattr("backend.services.creator.repair.complete_chat_once", fake_complete)
    review = await _run_script_responsibility_review(
        file_path=spec.path,
        script_content="def run(payload):\n    return {'image_path': 'out.png'}\n",
        skill_plan_entry=spec,
        requirements=[req],
    )
    assert review["passed"] is False
    assert review.get("failure_type") != "script_requirement_validator_incomplete"
    assert review["issues"][0]["details"]["issue_type"] == "semantic_action_incomplete"


@pytest.mark.asyncio
async def test_image_artifact_empty_file_semantic_fail_enters_script_patch(monkeypatch):
    spec = _script_spec(
        inputs=["image prompt"],
        outputs=["real image artifact"],
        purpose="根据输入生成真实图片 artifact。",
    )
    req = build_default_requirement_graph([spec]).requirements[0]

    async def fake_complete(*args, **kwargs):
        return json.dumps({
            "passed": False,
            "blocking_issues": [{
                "issue_type": "semantic_action_incomplete",
                "scope": "current_file_only",
                "failure_layer": "responsibility",
                "failed_file": spec.path,
                "semantic_failure": "current script only creates an empty file path and does not perform its core responsibility",
                "problem": "no real artifact generation occurs",
                "minimal_edit": "generate the artifact instead of touching an empty file",
            }],
            "advisory_notes": [],
            "repair_instructions": "patch the current script generation logic",
        })

    monkeypatch.setattr("backend.services.creator.repair.complete_chat_once", fake_complete)
    script = """
def run(payload):
    path = str(payload.get('name', 'out')) + '.png'
    open(path, 'wb').close()
    return {'image_path': path}
"""
    review = await _run_script_responsibility_review(
        file_path=spec.path,
        script_content=script,
        skill_plan_entry=spec,
        requirements=[req],
    )
    assert review["passed"] is False
    assert review["issues"][0]["minimal_edit"] == "generate the artifact instead of touching an empty file"


def test_script_requirement_failed_uses_localized_patch_mode():
    from backend.services.creator.api import _repair_mode_for_first_round

    assert _repair_mode_for_first_round(
        source="script_requirement_failed",
        file_path="scripts/generic.py",
        attempt=1,
    ) == "localized_patch"


@pytest.mark.asyncio
async def test_generate_file_empty_image_semantic_fail_enters_localized_patch(monkeypatch, tmp_path):
    from backend.config import settings
    from backend.services.creator import api
    from backend.services.creator.common import GenerateFileRequest

    monkeypatch.setattr(settings, "skills_path", tmp_path)
    (tmp_path / "demo-skill").mkdir()

    spec = _script_spec(path="scripts/main.py", purpose="根据输入生成真实图片 artifact。")
    reviews = iter([
        {
            "passed": False,
            "failure_type": "script_requirement_failed",
            "issues": [{
                "id": "script_functional.responsibility",
                "failed_file": "scripts/main.py",
                "reason": "current script only creates an empty file path and does not perform its core responsibility",
                "minimal_edit": "generate the artifact instead of touching an empty file",
            }],
            "repair_instructions": "patch current script generation logic",
        },
        {"passed": True, "issues": []},
    ])
    repair_modes = []

    async def fake_complete_creator_file_generation(**_kwargs):
        return "def run(payload):\n    path = str(payload.get('name', 'out')) + '.png'\n    open(path, 'wb').close()\n    return {'image_path': path}\n"

    async def fake_responsibility_review(**_kwargs):
        return next(reviews)

    async def fake_repair_generated_file_with_feedback(**kwargs):
        repair_modes.append(kwargs.get("repair_mode"))
        assert "current script only creates an empty file path" in kwargs.get("validation_error", "")
        return "def run(payload):\n    return {'image_path': 'generated.png'}\n"

    monkeypatch.setattr(api, "_complete_creator_file_generation", fake_complete_creator_file_generation)
    monkeypatch.setattr(api, "_run_script_responsibility_review", fake_responsibility_review)
    monkeypatch.setattr(api, "_repair_generated_file_with_feedback", fake_repair_generated_file_with_feedback)
    monkeypatch.setattr(api, "_skill_plan_entry_for_file", lambda **_kwargs: spec)
    async def fake_generated_file_validator_round(**_kwargs):
        return {"passed": False, "issues": []}

    monkeypatch.setattr(api, "_run_generated_file_validator_round", fake_generated_file_validator_round)
    monkeypatch.setattr(api, "_check_script_content_review_contract", lambda *args, **kwargs: [])

    response = await api.generate_file(GenerateFileRequest(
        skill_name="demo-skill",
        file_path="scripts/main.py",
        purpose="generate image",
        blueprint_text="scripts/main.py",
        conversation_history=[],
        role="generic_script",
        skill_plan_entry=spec.model_dump(mode="json"),
    ))
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
    body = "".join(chunks)

    assert repair_modes == ["localized_patch"], body
    assert "file_done" in body


def test_markdown_format_rewrite_detection_uses_structured_contract_not_message_text():
    from backend.services.creator.api import is_markdown_hard_format_error
    from backend.services.creator.common import FileGenerationStageError
    from backend.services.creator.contracts import ContractCheckResult, ContractValidationError

    result = ContractCheckResult(
        id="opaque.format.id",
        passed=False,
        target="SKILL.md",
        message="opaque localized message",
        expected="opaque expected value",
        minimal_edit="rewrite the whole generated file",
        details={"repair_strategy": "full_rewrite", "model_patch_allowed": False},
        layer="markdown_contract",
    )
    stage_error = FileGenerationStageError(
        source="content_review",
        layer="markdown_contract",
        detail="opaque text without markdown ids or severity words",
        original=ContractValidationError("opaque", [result]),
    )

    assert is_markdown_hard_format_error(stage_error) is True


@pytest.mark.asyncio
async def test_generate_file_format_stage_regenerates_before_responsibility_or_patch(monkeypatch, tmp_path):
    from backend.config import settings
    from backend.services.creator import api
    from backend.services.creator.common import GenerateFileRequest

    monkeypatch.setattr(settings, "skills_path", tmp_path)
    (tmp_path / "demo-skill").mkdir()

    spec = _script_spec(path="scripts/main.py")
    calls = iter([
        "```python\nprint('one')\n```\n```python\nprint('two')\n```",
        "def run(payload):\n    return {'artifact': payload}\n",
    ])
    responsibility_calls = []

    async def fake_complete_creator_file_generation(**_kwargs):
        return next(calls)

    async def fake_responsibility_review(**_kwargs):
        responsibility_calls.append(_kwargs["script_content"])
        return {"passed": True, "issues": []}

    async def fail_patch(**_kwargs):
        raise AssertionError("FORMAT_STAGE failure must not enter patch")

    monkeypatch.setattr(api, "_complete_creator_file_generation", fake_complete_creator_file_generation)
    monkeypatch.setattr(api, "_run_script_responsibility_review", fake_responsibility_review)
    monkeypatch.setattr(api, "_repair_generated_file_with_feedback", fail_patch)
    monkeypatch.setattr(api, "_skill_plan_entry_for_file", lambda **_kwargs: spec)

    response = await api.generate_file(GenerateFileRequest(
        skill_name="demo-skill",
        file_path="scripts/main.py",
        purpose="generate report",
        blueprint_text="scripts/main.py",
        conversation_history=[],
        role="generic_script",
        skill_plan_entry=spec.model_dump(mode="json"),
    ))
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
    body = "".join(chunks)

    assert "regenerating" in body
    assert responsibility_calls == ["def run(payload):\n    return {'artifact': payload}"], body


@pytest.mark.asyncio
async def test_generate_file_responsibility_patch_feedback_is_stage_isolated(monkeypatch, tmp_path):
    from backend.config import settings
    from backend.services.creator import api
    from backend.services.creator.common import GenerateFileRequest

    monkeypatch.setattr(settings, "skills_path", tmp_path)
    (tmp_path / "demo-skill").mkdir()

    spec = _script_spec(path="scripts/main.py")
    reviews = iter([
        {
            "passed": False,
            "failure_type": "script_requirement_failed",
            "issues": [{
                "id": "script_requirement_failed",
                "failed_file": "scripts/main.py",
                "reason": "semantic responsibility is absent",
                "minimal_edit": "implement responsibility",
            }],
        },
        {"passed": True, "issues": []},
    ])
    feedbacks = []
    validator_calls = []

    async def fake_complete_creator_file_generation(**_kwargs):
        return "def run(payload):\n    return {'artifact': 'fixed'}\n"

    async def fake_responsibility_review(**_kwargs):
        return next(reviews)

    async def fake_validator_round(**_kwargs):
        validator_calls.append(_kwargs)
        return {"passed": False, "issues": []}

    async def fake_repair_generated_file_with_feedback(**kwargs):
        feedbacks.append(kwargs.get("validation_error", ""))
        return "def run(payload):\n    return {'artifact': payload}\n"

    monkeypatch.setattr(api, "_complete_creator_file_generation", fake_complete_creator_file_generation)
    monkeypatch.setattr(api, "_run_script_responsibility_review", fake_responsibility_review)
    monkeypatch.setattr(api, "_run_generated_file_validator_round", fake_validator_round)
    monkeypatch.setattr(api, "_repair_generated_file_with_feedback", fake_repair_generated_file_with_feedback)
    monkeypatch.setattr(api, "_skill_plan_entry_for_file", lambda **_kwargs: spec)

    response = await api.generate_file(GenerateFileRequest(
        skill_name="demo-skill",
        file_path="scripts/main.py",
        purpose="generate report",
        blueprint_text="scripts/main.py",
        conversation_history=[],
        role="generic_script",
        skill_plan_entry=spec.model_dump(mode="json"),
    ))
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))

    assert validator_calls == []
    assert feedbacks
    feedback = feedbacks[0]
    assert "RESPONSIBILITY_PATCH_STAGE" in feedback
    assert "semantic responsibility is absent" in feedback
    assert "argv" not in feedback
    assert "stdout" not in feedback
    assert "artifact" not in feedback
    assert "E2E" not in feedback
    assert "格式" not in feedback
    assert "字段" not in feedback
    assert "运行" not in feedback
    assert "跨文件" not in feedback
    assert "表达" not in feedback


@pytest.mark.asyncio
async def test_responsibility_prompt_omits_non_responsibility_counterexamples(monkeypatch):
    captured_messages = []
    spec = _script_spec(path="scripts/main.py", purpose="complete the requested task")

    async def fake_complete(messages, model):
        captured_messages.extend(messages)
        return '{"passed": true, "blocking_issues": [], "advisory_notes": []}'

    monkeypatch.setattr("backend.services.creator.repair.complete_chat_once", fake_complete)

    await _run_script_responsibility_review(
        file_path=spec.path,
        script_content="def run(payload):\n    return {'result': payload}\n",
        skill_plan_entry=spec,
        requirements=[],
        review_context={"phase": "RESPONSIBILITY_STAGE", "policy": "只判断当前文件职责是否完成。"},
    )

    prompt_text = "\n".join(str(message.get("content") or "") for message in captured_messages)
    assert "只判断当前脚本是否完成自身职责" in prompt_text
    assert "不要求固定字段名" in prompt_text
    assert "语义职责槽位参考" in prompt_text
    for forbidden in ["argv", "stdout", "artifact", "E2E", "跨文件"]:
        assert forbidden not in prompt_text


def test_error_stdout_bypass_cannot_satisfy_expected_outputs():
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    issues = detect_error_stdout_bypass('try:\n    run()\nexcept Exception:\n    return {"error": "failed"}\n', [req], ["semantic artifact"])
    assert issues and issues[0]["requirement_id"] == req.id


@pytest.mark.parametrize(
    ("argv_template", "passed"),
    [
        ('{"descriptions":{{illustration_descriptions}}}', True),
        ('{"image_paths":{{image_paths}}}', True),
        ('{"topic":"{{user_request}}"}', True),
        ('{"x":{{bad syntax}}}', False),
        ('{"x":{{value}}', False),
    ],
)
def test_skill_md_command_args_parseable_accepts_json_value_placeholders(argv_template, passed):
    from backend.services.creator.contracts import _validate_command_is_single_shell_json_invocation
    from backend.services.skill_plan import build_skill_plan_entry

    entry = build_skill_plan_entry(
        file_path="scripts/main.py",
        purpose="role: generic_script inputs: payload outputs: result",
    )
    results = _validate_command_is_single_shell_json_invocation(
        command=f"python scripts/main.py '{argv_template}'",
        script_path="scripts/main.py",
        entry=entry,
    )
    args_check = next(result for result in results if result.id == "skill_md.command_block.args_parseable")

    assert args_check.passed is passed


@pytest.mark.asyncio
async def test_initial_markdown_generation_rewrites_bad_metadata_before_body(monkeypatch):
    from backend.services.creator import api

    calls = []

    async def fake_complete_creator_file_generation(**kwargs):
        calls.append(kwargs["prompt_variant"])
        if kwargs["prompt_variant"] == "generate_markdown_metadata_region":
            return "---\nname: demo\ndescription: bad\n"
        if kwargs["prompt_variant"] == "rewrite_markdown_metadata_region":
            return "---\nname: demo\ndescription: ok\n---\n"
        if kwargs["prompt_variant"] == "generate_markdown_body_region":
            return "# Body\n\nContent.\n"
        raise AssertionError(kwargs["prompt_variant"])

    monkeypatch.setattr(api, "_complete_creator_file_generation", fake_complete_creator_file_generation)
    result = await api._generate_markdown_initial_regions(
        file_path="SKILL.md",
        skill_name="demo",
        purpose="demo",
        blueprint_text="demo",
        model="test-model",
    )

    assert calls == [
        "generate_markdown_metadata_region",
        "rewrite_markdown_metadata_region",
        "generate_markdown_body_region",
    ]
    assert result.startswith("---\nname: demo\ndescription: ok\n---")
    assert "# Body" in result


def test_reference_markdown_warning_uses_reference_content_warning():
    from backend.services.creator import api

    assert api._markdown_warning_error_type("references/guide.md") == "reference_content_warning"
    assert api._markdown_warning_error_type("SKILL.md") == "md_format_warning"


@pytest.mark.asyncio
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



@pytest.mark.asyncio
async def test_generate_file_validator_incomplete_uses_entry_requirements_static_fallback(monkeypatch, tmp_path):
    from backend.config import settings
    from backend.services.creator import api
    from backend.services.creator.common import GenerateFileRequest

    monkeypatch.setattr(settings, "skills_path", tmp_path)
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\ndescription: demo\n---\n", encoding="utf-8")

    spec = _script_spec(path="scripts/main.py", inputs=["customer brief"], outputs=["report path"])
    req = build_default_requirement_graph([spec]).requirements[0]
    spec.requirements = [req]
    entry = spec.model_dump(mode="json")
    entry["requirements"] = [req.model_dump(mode="json")]

    async def fake_complete_creator_file_generation(**_kwargs):
        return "def run(payload):\n    return {'path': 'fixed.pdf'}\n"

    reviews = iter([
        {"passed": False, "failure_type": "script_requirement_validator_incomplete", "issues": []},
        {"passed": True, "issues": []},
    ])

    async def fake_responsibility_review(**_kwargs):
        return next(reviews)

    async def fake_repair_generated_file_with_feedback(**kwargs):
        raise AssertionError("validator protocol failure must not repair Python script")

    monkeypatch.setattr(api, "_complete_creator_file_generation", fake_complete_creator_file_generation)
    monkeypatch.setattr(api, "_run_script_responsibility_review", fake_responsibility_review)
    monkeypatch.setattr(api, "_load_persisted_requirement_graph", lambda _skill_name: None)
    monkeypatch.setattr(api, "_skill_plan_entry_for_file", lambda **_kwargs: spec)
    monkeypatch.setattr(api, "_repair_generated_file_with_feedback", fake_repair_generated_file_with_feedback)
    async def fake_generated_file_validator_round(**_kwargs):
        return {"passed": False, "issues": []}

    monkeypatch.setattr(api, "_run_generated_file_validator_round", fake_generated_file_validator_round)
    monkeypatch.setattr(api, "_check_script_content_review_contract", lambda *args, **kwargs: [])

    response = await api.generate_file(GenerateFileRequest(
        skill_name="demo-skill",
        file_path="scripts/main.py",
        purpose="generate report",
        blueprint_text="scripts/main.py",
        conversation_history=[],
        role="generic_script",
        skill_plan_entry=entry,
    ))
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
    body = "".join(chunks)

    assert "script_requirement_validator_incomplete" in body
    assert "file_done" in body


@pytest.mark.asyncio
async def test_generate_file_tool_contract_mismatch_enters_repair_not_error(monkeypatch, tmp_path):
    from backend.config import settings
    from backend.services.creator import api
    from backend.services.creator.common import GenerateFileRequest

    monkeypatch.setattr(settings, "skills_path", tmp_path)
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\ndescription: demo\n---\n", encoding="utf-8")

    spec = _script_spec(path="scripts/main.py")
    req = build_default_requirement_graph([spec]).requirements[0]
    entry = spec.model_dump(mode="json")
    entry["requirements"] = [req.model_dump(mode="json")]

    async def fake_complete_creator_file_generation(**_kwargs):
        return "from backend.services.runtime_tools import missing_runtime_helper\n\ndef run(payload):\n    return missing_runtime_helper(payload)\n"

    reviews = iter([
        {"passed": False, "failure_type": "script_requirement_validator_incomplete", "issues": []},
        {"passed": True, "issues": []},
    ])

    async def fake_responsibility_review(**_kwargs):
        return next(reviews)

    async def fake_repair_generated_file_with_feedback(**kwargs):
        assert kwargs.get("file_path") == "scripts/main.py"
        assert "tool_contract_mismatch" in kwargs.get("validation_error", "")
        return "def run(payload):\n    return {'artifact': {'ok': True}}\n"

    monkeypatch.setattr(api, "_complete_creator_file_generation", fake_complete_creator_file_generation)
    monkeypatch.setattr(api, "_run_script_responsibility_review", fake_responsibility_review)
    monkeypatch.setattr(api, "_load_persisted_requirement_graph", lambda _skill_name: None)
    monkeypatch.setattr(api, "_skill_plan_entry_for_file", lambda **_kwargs: spec)
    async def fake_generated_file_validator_round(**_kwargs):
        return {"passed": False, "issues": []}

    monkeypatch.setattr(api, "_repair_generated_file_with_feedback", fake_repair_generated_file_with_feedback)
    monkeypatch.setattr(api, "_run_generated_file_validator_round", fake_generated_file_validator_round)
    monkeypatch.setattr(api, "_check_script_content_review_contract", lambda *args, **kwargs: [])

    response = await api.generate_file(GenerateFileRequest(
        skill_name="demo-skill",
        file_path="scripts/main.py",
        purpose="generate report",
        blueprint_text="scripts/main.py",
        conversation_history=[],
        role="generic_script",
        skill_plan_entry=entry,
    ))
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
    body = "".join(chunks)

    assert "tool_contract_mismatch" in body
    assert "repairing" in body
    assert "script_requirement_validator_incomplete" not in body

def test_validator_error_stage_returns_frontend_error_not_script_repair():
    from backend.services.creator import api

    import inspect
    source = inspect.getsource(api.generate_file)
    assert "script_requirement_validator_error" in source
    assert "script_requirement_validator_incomplete" in source
    assert "_file_done_error_sse" in source
    assert "single_file_production_validation" not in source
    assert "auto-repair the current file instead of returning directly to the frontend" not in source


def test_validator_graph_source_quality_marked(monkeypatch):
    import asyncio
    from backend.services.creator import api
    from backend.services.platform_io_contract import build_platform_io_contract

    async def fake_complete(messages, model):
        boundary = build_platform_io_contract()["platform_skill_boundary"]
        return json.dumps({
            "function_items": [{"target_file": "scripts/generic.py", "role": "script", "purpose": "Transform the provided semantic input into the declared artifact.", "inputs": ["semantic source"], "outputs": ["semantic artifact"], "required_capabilities": ["generic_generation"], "constraints": []}],
            "responsibility_edges": [
                {"from_node": "platform_input_node", "from_output": boundary["input_envelope_fields"][0], "to_node": "scripts/generic.py", "to_input": "semantic source", "purpose": "input", "constraints": []},
                {"from_node": "scripts/generic.py", "from_output": "semantic artifact", "to_node": "platform_output_node", "to_input": boundary["final_output_fields"][0], "purpose": "output", "constraints": []},
            ],
        })

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
    graph = asyncio.run(api._extract_requirement_graph_with_validator(blueprint_text="blueprint", files_out=[_script_spec()], requested_model=None))
    assert graph.dataflow_edges


def test_initial_graph_missing_platform_io_enters_refine(monkeypatch):
    import asyncio
    from backend.services.creator import api
    from backend.services.platform_io_contract import build_platform_io_contract

    boundary = build_platform_io_contract()["platform_skill_boundary"]
    calls = []

    async def fake_complete(messages, model):
        calls.append(messages)
        return json.dumps({
            "function_items": [{
                "target_file": "scripts/generic.py",
                "role": "generic_script",
                "purpose": "Transform input.",
                "inputs": ["semantic source"],
                "outputs": ["semantic artifact"],
                "required_capabilities": [],
                "constraints": [],
            }],
            "responsibility_edges": [
                {"from_node": "platform_input_node", "from_output": boundary["input_envelope_fields"][0], "to_node": "scripts/generic.py", "to_input": "semantic source", "purpose": "provide input", "constraints": []},
                {"from_node": "scripts/generic.py", "from_output": "semantic artifact", "to_node": "platform_output_node", "to_input": boundary["final_output_fields"][0], "purpose": "deliver output", "constraints": []},
            ],
        })

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
    graph = asyncio.run(api._extract_requirement_graph_with_validator(
        blueprint_text="blueprint",
        files_out=[_script_spec()],
        responsibility_edges=[],
        function_items=[{"target_file": "scripts/generic.py", "role": "generic_script", "purpose": "Transform input.", "inputs": ["semantic source"], "outputs": ["semantic artifact"], "required_capabilities": [], "constraints": []}],
    ))

    assert calls
    assert graph.dataflow_edges


def test_initial_graph_non_refinable_structure_error_fails_directly(monkeypatch):
    import asyncio
    from backend.services.creator import api

    async def fake_complete(*_args, **_kwargs):
        raise AssertionError("refine should not run for non-platform structural errors")

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
    with pytest.raises(RequirementGraphValidationError):
        asyncio.run(api._extract_requirement_graph_with_validator(
            blueprint_text="blueprint",
            files_out=[_script_spec()],
            responsibility_edges=[
                {"from_node": "platform_input_node", "from_output": "user_request", "to_node": "scripts/generic.py", "to_input": "semantic source", "purpose": "input", "constraints": []},
                {"from_node": "scripts/generic.py", "from_output": "semantic artifact", "to_node": "platform_output_node", "to_input": "text", "purpose": "output", "constraints": []},
                {"from_node": "scripts/missing.py", "from_output": "x", "to_node": "scripts/generic.py", "to_input": "semantic source", "purpose": "bad", "constraints": []},
            ],
            function_items=[{"target_file": "scripts/generic.py", "role": "generic_script", "purpose": "Transform input.", "inputs": ["semantic source"], "outputs": ["semantic artifact"], "required_capabilities": [], "constraints": []}],
        ))


def test_graph_refine_rejects_unrelated_function_item_change():
    from backend.services.creator import api

    original = build_default_requirement_graph(
        [_script_spec(path="scripts/a.py"), _script_spec(path="scripts/b.py")],
        responsibility_edges=[],
        function_items=[
            {"target_file": "scripts/a.py", "role": "generic_script", "purpose": "A", "inputs": ["in"], "outputs": ["mid"], "required_capabilities": [], "constraints": []},
            {"target_file": "scripts/b.py", "role": "generic_script", "purpose": "B", "inputs": ["mid"], "outputs": ["out"], "required_capabilities": [], "constraints": []},
        ],
    )
    refined = build_default_requirement_graph(
        [_script_spec(path="scripts/a.py"), _script_spec(path="scripts/b.py")],
        responsibility_edges=[],
        function_items=[
            {"target_file": "scripts/a.py", "role": "generic_script", "purpose": "A changed", "inputs": ["in"], "outputs": ["mid"], "required_capabilities": [], "constraints": []},
            {"target_file": "scripts/b.py", "role": "generic_script", "purpose": "B", "inputs": ["mid"], "outputs": ["out"], "required_capabilities": [], "constraints": []},
        ],
    )

    with pytest.raises(ValueError, match="unrelated FunctionItem"):
        api._validate_refined_graph_scope(
            before=original,
            after=refined,
            feedback={"related_nodes": ["platform_input_node"]},
        )


def test_graph_refine_retries_invalid_output_then_succeeds(monkeypatch):
    import asyncio
    from backend.services.creator import api
    from backend.services.platform_io_contract import build_platform_io_contract

    boundary = build_platform_io_contract()["platform_skill_boundary"]
    calls = []

    async def fake_complete(messages, model):
        calls.append(messages)
        if len(calls) == 1:
            return "not json"
        return json.dumps({
            "function_items": [{
                "target_file": "scripts/generic.py",
                "role": "generic_script",
                "purpose": "Transform input.",
                "inputs": ["semantic source"],
                "outputs": ["semantic artifact"],
                "required_capabilities": [],
                "constraints": [],
            }],
            "responsibility_edges": [
                {"from_node": "platform_input_node", "from_output": boundary["input_envelope_fields"][0], "to_node": "scripts/generic.py", "to_input": "semantic source", "purpose": "provide input", "constraints": []},
                {"from_node": "scripts/generic.py", "from_output": "semantic artifact", "to_node": "platform_output_node", "to_input": boundary["final_output_fields"][0], "purpose": "deliver output", "constraints": []},
            ],
        })

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
    graph = asyncio.run(api._extract_requirement_graph_with_validator(
        blueprint_text="blueprint",
        files_out=[_script_spec()],
        responsibility_edges=[],
        function_items=[{"target_file": "scripts/generic.py", "role": "generic_script", "purpose": "Transform input.", "inputs": ["semantic source"], "outputs": ["semantic artifact"], "required_capabilities": [], "constraints": []}],
    ))

    assert len(calls) == 2
    assert graph.dataflow_edges


def test_graph_refine_invalid_output_reaches_retry_limit(monkeypatch):
    import asyncio
    from backend.services.creator import api

    calls = []

    async def fake_complete(_messages, _model):
        calls.append(1)
        return "not json"

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
    with pytest.raises(RequirementGraphValidationError):
        asyncio.run(api._extract_requirement_graph_with_validator(
            blueprint_text="blueprint",
            files_out=[_script_spec()],
            responsibility_edges=[],
            function_items=[{"target_file": "scripts/generic.py", "role": "generic_script", "purpose": "Transform input.", "inputs": ["semantic source"], "outputs": ["semantic artifact"], "required_capabilities": [], "constraints": []}],
        ))
    assert len(calls) == 3


def test_fallback_graph_source_quality_marked():
    graph = build_default_requirement_graph([_script_spec()])
    assert graph.requirement_graph_source in {"fallback", "deterministic_file_contracts"}
    assert graph.requirement_graph_quality in {"fallback_coarse", "normalized_responsibility"}


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


def test_script_sanitize_extracts_single_fenced_block_with_prose_for_repair_input():
    from backend.services.creator.generation import _sanitize_generated_file_content

    raw = "Here is the file:\n```text\n#!/usr/bin/env sh\necho ok\n```\nDone."
    assert _sanitize_generated_file_content("scripts/run.sh", raw) == "#!/usr/bin/env sh\necho ok"


def test_script_repair_fenced_block_is_recanonicalized_to_pure_script():
    from backend.services.creator.api import _canonicalize_generated_candidate

    raw = "Fixed version:\n```text\n#!/usr/bin/env node\nconsole.log('ok')\n```"
    assert _canonicalize_generated_candidate(
        file_path="scripts/run",
        content=raw,
        skill_plan_entry={},
    ) == "#!/usr/bin/env node\nconsole.log('ok')"


def test_passed_true_missing_checks_is_advisory_not_business_repair():
    from backend.services.creator.repair import _parse_requirement_review_result

    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    review = _parse_requirement_review_result(
        {"passed": True, "advisory_notes": ["validator omitted optional structure"]},
        requirements=[req],
        file_path=req.target_file,
    )
    assert review["passed"] is True
    assert review["issues"] == []
    assert review["failure_type"] == "script_requirement_validator_incomplete"


def test_passed_false_missing_checks_with_structured_semantic_blocker_fails():
    from backend.services.creator.repair import _parse_requirement_review_result

    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    review = _parse_requirement_review_result(
        {
            "passed": False,
            "blocking_issues": [{
                "failed_file": req.target_file,
                "scope": "current_file_only",
                "failure_layer": "responsibility",
                "semantic_failure": "core semantic construction is absent",
                "minimal_edit": "Implement the current file semantic construction.",
            }],
        },
        requirements=[req],
        file_path=req.target_file,
    )
    assert review["passed"] is False
    assert review["failure_type"] == "script_requirement_failed"
    assert review["issues"][0]["semantic_failure"] == "core semantic construction is absent"


def test_missing_checks_does_not_swallow_current_file_semantic_failure():
    from backend.services.creator.repair import _parse_requirement_review_result

    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    review = _parse_requirement_review_result(
        {
            "passed": True,
            "issues": [{
                "target_file": req.target_file,
                "scope": "current_file",
                "failure_layer": "semantic_responsibility",
                "semantic_failure": "tool result is not used to construct the output",
                "repair_target_file": req.target_file,
            }],
        },
        requirements=[req],
        file_path=req.target_file,
    )
    assert review["passed"] is False
    assert review["issues"][0]["semantic_failure"] == "tool result is not used to construct the output"


def test_missing_checks_with_interface_only_blocking_issue_is_advisory():
    from backend.services.creator.repair import _parse_requirement_review_result

    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    review = _parse_requirement_review_result(
        {
            "passed": True,
            "blocking_issues": [{
                "failed_file": req.target_file,
                "scope": "current_file_only",
                "failure_layer": "responsibility",
                "interface_notes": ["interface mapping advisory"],
                "minimal_edit": "Do not force this as a semantic repair.",
            }],
        },
        requirements=[req],
        file_path=req.target_file,
    )
    assert review["passed"] is True
    assert review["issues"] == []
    assert review["failure_type"] == "script_requirement_validator_incomplete"


def test_checks_structured_semantic_blocker_fails():
    from backend.services.creator.repair import _parse_requirement_review_result

    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    review = _parse_requirement_review_result(
        {
            "passed": False,
            "checks": [{
                "requirement_id": req.id,
                "failed_file": req.target_file,
                "scope": "current_file_only",
                "failure_layer": "responsibility",
                "semantic_failure": "input meaning never reaches the output",
                "evidence_level": "missing",
                "missing_evidence": ["current file semantic path"],
            }],
        },
        requirements=[req],
        file_path=req.target_file,
    )
    assert review["passed"] is False
    assert review["issues"][0]["semantic_failure"] == "input meaning never reaches the output"


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




def test_reference_contract_checks_own_semantic_purpose_not_fixed_sections():
    from backend.services.creator.contracts import _check_reference_file_contract

    good = """---
title: Tone Guide
description: Brand voice reference
---
# Voice Notes

## Narrative cues
- Brand voice should stay concise, warm, and evidence-oriented.
- Typography examples should distinguish heading, label, and body usage.
- Use contrast rules and spacing notes when adapting layouts.

## Review hints
Confirm that every sample paragraph follows the brand voice and typography constraints.
"""
    good_failed = {r.id for r in _check_reference_file_contract("references/tone.md", good, purpose="brand voice typography constraints") if not r.passed}
    assert "reference.content.covers_own_semantic_purpose" not in good_failed

    generic = """---
title: Generic Guide
description: Generic reference
---
# General Notes

## Rules
- Provide useful information.
- Keep the content organized.
- Include examples when needed.

## Checks
Make sure the document is clear and complete for future users.
"""
    generic_failed = {r.id for r in _check_reference_file_contract("references/tone.md", generic, purpose="brand voice typography constraints") if not r.passed}
    assert "reference.content.covers_own_semantic_purpose" in generic_failed


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


def test_required_missing_blocking_not_downgraded_by_message_text():
    from backend.services.creator.repair import _parse_requirement_review_result
    req = build_default_requirement_graph([_script_spec()]).requirements[0]
    review = _parse_requirement_review_result(
        {"passed": False, "checks": [{"requirement_id": req.id, "scope": "current_file_only", "failure_layer": "responsibility", "semantic_failure": "required responsibility is missing", "status": "failed", "severity": "blocking", "blocking": True, "evidence_level": "missing", "missing_evidence": ["core evidence"], "reason": "字段名旁边的 required responsibility missing"}]},
        requirements=[req],
        file_path=req.target_file,
    )
    assert review["passed"] is False
    assert review["issues"][0]["requirement_id"] == req.id


def test_validator_failure_repair_instructions_do_not_enter_feedback():
    from backend.services.creator.repair import _format_file_validator_feedback
    for failure_type in ["none", "script_requirement_validator_error", "script_requirement_validator_incomplete", "validator_error", "validator_incomplete"]:
        feedback = _format_file_validator_feedback(
            "deterministic failure",
            {"failure_type": failure_type, "repair_instructions": "rename output", "issues": []},
            file_path="scripts/generic.py",
        )
        assert "rename output" not in feedback


def test_reference_sanitizer_does_not_template_replace_and_preserves_fence():
    from backend.services.creator.contracts import _sanitize_reference_placeholders
    content = "---\ntitle: Guide\ndescription: Demo\n---\n# Guide\nTODO\n保留前缀 placeholder 保留后缀\n```text\nTODO stays in fence\n```\n"
    sanitized = _sanitize_reference_placeholders(content)
    assert sanitized.startswith("---\ntitle: Guide\ndescription: Demo\n---")
    assert "具体规则、示例和约束" not in sanitized
    assert "保留前缀" in sanitized and "保留后缀" in sanitized
    assert "TODO stays in fence" in sanitized
    assert "\nTODO\n" not in sanitized


def test_contract_check_format_omits_reference_banned_terms_from_details():
    from backend.services.creator.contracts import _check_reference_file_contract, _format_contract_checks
    content = "---\ntitle: Guide\ndescription: Demo\n---\n# Guide\nTODO 待补充\n"
    failed = [r for r in _check_reference_file_contract("references/guide.md", content) if not r.passed]
    text = _format_contract_checks(failed, passed=False)
    assert "context_hash" in text
    assert "待补充" not in text


def test_structured_failure_signature_ignores_candidate_digest():
    from backend.services.creator import api
    from backend.services.creator.common import FileGenerationStageError
    err1 = FileGenerationStageError(source="script_functional", layer="responsibility", detail="one")
    err2 = FileGenerationStageError(source="script_functional", layer="responsibility", detail="one")
    assert api._structured_failure_signature(err1, "same failure") == api._structured_failure_signature(err2, "same failure")


def test_skill_md_format_stage_no_longer_blocks_bad_frontmatter():
    from backend.services.creator import api

    assert api._first_round_format_stage_error(
        file_path="SKILL.md",
        content="# Runtime instructions only\n\n```bash\npython scripts/main.py '{}'\n```",
    ) is None


@pytest.mark.asyncio
async def test_write_file_accepts_skill_md_without_frontmatter(monkeypatch, tmp_path):
    from backend.services.creator import api

    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    (tmp_path / "demo").mkdir()

    response = await api.write_file(api.WriteFileRequest(
        skill_name="demo",
        file_path="SKILL.md",
        content="# Runtime instructions only\n\n```bash\npython scripts/main.py '{}'\n```",
    ))

    assert response.success is True
    assert (tmp_path / "demo" / "SKILL.md").read_text(encoding="utf-8").startswith("# Runtime instructions only")


def _generic_three_script_specs():
    return [
        _script_spec(
            path="scripts/source.py",
            purpose="Produce source semantic units.",
            inputs=["semantic input"],
            outputs=["source semantic units"],
        ),
        _script_spec(
            path="scripts/transform.py",
            purpose="Transform semantic units.",
            inputs=["source semantic units"],
            outputs=["transformed semantic units"],
        ),
        _script_spec(
            path="scripts/assemble.py",
            purpose="Assemble transformed semantic units.",
            inputs=["transformed semantic units"],
            outputs=["assembled semantic result"],
        ),
    ]


def test_arbitrary_constraint_projects_from_filespec_to_requirement_item():
    spec = _script_spec(
        constraints=[{
            "name": "image_count_relationship",
            "kind": "responsibility_constraint",
            "value": {"expected": "one caption per image"},
            "comparator": "describes",
            "unit": "",
            "required": True,
        }]
    )
    req = build_default_requirement_graph([spec]).requirements[0]
    assert len(req.constraints) == 1
    assert req.constraints[0].name == "image_count_relationship"
    assert req.constraints[0].value == {"expected": "one caption per image"}


def test_arbitrary_constraints_share_same_transport_path():
    spec = _script_spec(
        constraints=[
            {"name": "font_size", "kind": "typography", "value": 14, "comparator": "describes", "required": True},
            {"name": "quality_threshold", "kind": "evaluation", "value": {"minimum": "high"}, "comparator": "describes", "required": True},
        ]
    )
    req = build_default_requirement_graph([spec]).requirements[0]
    assert [(c.name, c.kind, c.value) for c in req.constraints] == [
        ("font_size", "typography", 14),
        ("quality_threshold", "evaluation", {"minimum": "high"}),
    ]


def test_producer_payload_includes_only_current_target_constraints():
    from backend.services.creator.generation import _script_responsibility_requirements_payload

    graph = build_default_requirement_graph([
        _script_spec(path="scripts/a.py", constraints=[{"name": "a_only", "kind": "layout", "value": "grid", "comparator": "describes"}]),
        _script_spec(path="scripts/b.py", constraints=[{"name": "b_only", "kind": "frequency", "value": "daily", "comparator": "describes"}]),
    ])
    payload = _script_responsibility_requirements_payload(file_path="scripts/a.py", requirements=graph.requirements)
    assert len(payload) == 1
    assert payload[0]["target_file"] == "scripts/a.py"
    assert [c["name"] for c in payload[0]["constraints"]] == ["a_only"]


def test_runtime_and_artifact_contracts_do_not_become_semantic_must_do():
    spec = _script_spec(
        runtime_contract={"command_args": {"topic": "{{topic}}"}},
        artifact_contract={"stdout_fields": ["semantic artifact"], "final": True},
    )
    req = build_default_requirement_graph([spec]).requirements[0]
    joined = "\n".join(req.must_do)
    assert "Honor runtime_contract" not in joined
    assert "Honor artifact_contract" not in joined
    assert all(constraint.source != "default_contract" for constraint in req.constraints)


def test_internal_blueprint_constraints_flow_to_skillplan_and_filespec():
    from backend.services.blueprint_parser import parse_blueprint

    internal_blueprint_text = '''
📋 Skill 架构蓝图
- **Skill 名称**: constraint-flow
- scripts/: `scripts/render.py`
  scripts/render.py
  role: generic_script
  inputs: topic
  outputs: artifact
  constraints: [{"name":"image_count_relationship","kind":"responsibility","value":{"rule":"one caption per image"},"comparator":"describes","required":true}]
'''
    plan = parse_blueprint([{"role": "assistant", "content": internal_blueprint_text}])
    entry = next(item for item in plan.skill_plan.files if item.path == "scripts/render.py")
    assert entry.constraints == [{
        "name": "image_count_relationship",
        "kind": "responsibility",
        "value": {"rule": "one caption per image"},
        "comparator": "describes",
        "required": True,
    }]

    spec = FileSpecOut(
        path=entry.path,
        purpose=entry.purpose,
        required=entry.required,
        can_skip=entry.can_skip,
        file_type=entry.file_type,
        file_kind=entry.file_kind,
        role=entry.role,
        inputs=entry.inputs,
        outputs=entry.outputs,
        constraints=entry.constraints,
    )
    assert spec.constraints == entry.constraints


def test_producer_and_judge_requirement_payload_constraints_match():
    from backend.services.creator.common import RequirementConstraint, requirement_item_prompt_payload
    from backend.services.creator.generation import _script_responsibility_requirements_payload

    req = build_default_requirement_graph([_script_spec(path="scripts/current.py")]).requirements[0]
    req.constraints = [
        RequirementConstraint(
            name="layout_density",
            kind="layout",
            value={"max_items_per_row": 3},
            comparator="describes",
            required=False,
        )
    ]

    producer_payload = _script_responsibility_requirements_payload(
        file_path="scripts/current.py",
        requirements=[req],
    )[0]
    judge_payload = requirement_item_prompt_payload(req)

    assert producer_payload["constraints"] == judge_payload["constraints"]

    graph = build_default_requirement_graph([
        _script_spec(path="scripts/a.py", constraints=[{"name": "a_only", "kind": "layout", "value": "grid", "comparator": "describes"}]),
        _script_spec(path="scripts/b.py", constraints=[{"name": "b_only", "kind": "frequency", "value": "daily", "comparator": "describes"}]),
    ])
    payload = _script_responsibility_requirements_payload(file_path="scripts/a.py", requirements=graph.requirements)
    assert len(payload) == 1
    assert payload[0]["target_file"] == "scripts/a.py"
    assert [c["name"] for c in payload[0]["constraints"]] == ["a_only"]

def test_requirement_graph_persistence_helpers_round_trip(monkeypatch, tmp_path):
    from backend.services.creator import api

    monkeypatch.setattr(api.settings, "skills_path", tmp_path)

    graph = build_default_requirement_graph([_script_spec(path="scripts/main.py")])

    api._persist_requirement_graph("demo-skill", graph)
    api._persist_workflow_allocation_summary("demo-skill", "preserved constraints")

    loaded_graph = api._load_persisted_requirement_graph("demo-skill")
    loaded_summary = api._load_workflow_allocation_summary("demo-skill")

    assert loaded_graph is not None
    assert loaded_graph.requirements[0].target_file == "scripts/main.py"
    assert loaded_summary == "preserved constraints"


def test_responsibility_edges_parse_open_constraints():
    from backend.services.skill_plan import parse_responsibility_edges

    constraint = {
        "name": "alpha",
        "kind": "custom",
        "value": {"x": 1},
        "comparator": "describes",
        "required": True,
    }
    text = (
        'ResponsibilityEdges: '
        '[{"from_node":"scripts/a.py","from_output":"out","to_node":"scripts/b.py",'
        '"to_input":"inp","purpose":"handoff","constraints":['
        + __import__('json').dumps(constraint)
        + ']}]'
    )

    edges = parse_responsibility_edges(text)

    assert edges[0]["constraints"] == [constraint]


def test_responsibility_graph_preserves_planner_edges():
    specs = [
        _file_spec("scripts/a.py", inputs=["user_request"], outputs=["a_result"]),
        _file_spec("scripts/b.py", inputs=["a_result"], outputs=["final_response"]),
    ]
    edges = [
        {"from_node": "platform_input_node", "from_output": "user_request", "to_node": "scripts/a.py", "to_input": "user_request", "purpose": "input handoff", "constraints": []},
        {"from_node": "scripts/a.py", "from_output": "a_result", "to_node": "scripts/b.py", "to_input": "a_result", "purpose": "script handoff", "constraints": [{"name": "alpha", "kind": "custom", "value": {"x": 1}, "comparator": "describes", "required": True}]},
        {"from_node": "scripts/b.py", "from_output": "final_response", "to_node": "platform_output_node", "to_input": "final_response", "purpose": "final delivery", "constraints": []},
    ]

    graph = build_default_requirement_graph(specs, responsibility_edges=edges)

    assert graph.dataflow_edges == edges


def test_responsibility_graph_does_not_infer_edges_from_matching_io_names():
    specs = [
        _file_spec("scripts/a.py", outputs=["result"]),
        _file_spec("scripts/b.py", inputs=["result"]),
    ]

    graph = build_default_requirement_graph(specs)

    assert graph.dataflow_edges == []


def test_producer_and_judge_receive_same_function_item_graph_context():
    from backend.services.creator.common import function_item_graph_context

    specs = [
        _file_spec("scripts/a.py", inputs=["user_request"], outputs=["a_result"], constraints=[{"name": "local", "kind": "custom", "value": "keep", "comparator": "describes"}]),
        _file_spec("scripts/b.py", inputs=["a_result"], outputs=["final_response"]),
    ]
    edge_constraint = {"name": "alpha", "kind": "custom", "value": {"x": 1}, "comparator": "describes", "required": True}
    graph = build_default_requirement_graph(specs, responsibility_edges=[
        {"from_node": "scripts/a.py", "from_output": "a_result", "to_node": "scripts/b.py", "to_input": "a_result", "purpose": "handoff", "constraints": [edge_constraint]},
    ])

    producer_payload = function_item_graph_context(graph, "scripts/b.py")
    judge_payload = function_item_graph_context(graph, "scripts/b.py")

    assert producer_payload == judge_payload
    assert set(producer_payload) == {"function_item", "incoming_edges", "outgoing_edges"}
    assert producer_payload["function_item"]
    assert producer_payload["incoming_edges"][0]["constraints"] == [edge_constraint]


def test_producer_prompt_includes_real_local_edge_context():
    from backend.services.creator.generation import _build_generate_file_prompt

    specs = [
        _file_spec("scripts/a.py", outputs=["planner result"]),
        _file_spec("scripts/b.py", inputs=["consumed planner input"], outputs=["final_response"]),
    ]
    edge_constraint = {
        "name": "alpha",
        "kind": "custom",
        "value": {"x": 1},
        "comparator": "describes",
        "required": True,
    }
    graph = build_default_requirement_graph(specs, responsibility_edges=[
        {
            "from_node": "scripts/a.py",
            "from_output": "planner result",
            "to_node": "scripts/b.py",
            "to_input": "consumed planner input",
            "purpose": "carry upstream result with model-owned meaning",
            "constraints": [edge_constraint],
        }
    ])

    messages = _build_generate_file_prompt(
        "scripts/b.py",
        "demo-skill",
        "consume upstream result",
        "blueprint",
        [],
        role="generic_script",
        skill_plan_entry=specs[1].model_dump(mode="json"),
        requirements=graph.requirements,
        responsibility_graph=graph,
    )
    prompt_text = "\n".join(str(message.get("content") or "") for message in messages)

    assert "function_item_graph_context" in prompt_text
    assert "incoming_edges" in prompt_text
    assert "outgoing_edges" in prompt_text
    assert "carry upstream result with model-owned meaning" in prompt_text
    assert "alpha" in prompt_text


def test_structured_function_items_override_blueprint_script_responsibility_text():
    from backend.services.blueprint_parser import FileSpec, build_skill_plan_from_files
    structured = [{
        'target_file': 'scripts/a.py',
        'role': 'structured_role',
        'purpose': 'structured purpose',
        'inputs': ['semantic_input'],
        'outputs': ['semantic_result'],
        'required_capabilities': ['semantic_capability'],
        'constraints': [{'name': 'c', 'kind': 'generic', 'value': 'concrete_result', 'comparator': 'describes', 'required': True}],
    }]
    plan = build_skill_plan_from_files(
        skill_name='demo',
        files=[FileSpec(path='scripts/a.py', purpose='markdown purpose')],
        blueprint_text='purpose: markdown purpose\ninputs: [stale]\noutputs: [stale]',
        function_items=structured,
        responsibility_edges=[],
    )
    entry = next(item for item in plan.files if item.path == 'scripts/a.py')
    assert entry.role == 'structured_role'
    assert entry.purpose == 'structured purpose'
    assert entry.inputs == ['semantic_input']
    assert entry.outputs == ['semantic_result']
    assert entry.required_capabilities == ['semantic_capability']
    assert entry.constraints == structured[0]['constraints']


def test_responsibility_graph_uses_structured_function_items_directly():
    structured = [{
        'target_file': 'scripts/a.py',
        'role': 'structured_role',
        'purpose': 'structured purpose',
        'inputs': ['semantic_input'],
        'outputs': ['semantic_result'],
        'required_capabilities': ['semantic_capability'],
        'constraints': [],
    }]
    graph = build_default_requirement_graph([_script_spec(path='scripts/a.py', purpose='file purpose')], responsibility_edges=[], function_items=structured)
    item = graph.function_items[0]
    assert item.purpose == 'structured purpose'
    assert item.inputs == ['semantic_input']
    assert item.outputs == ['semantic_result']
    assert item.required_tools == ['semantic_capability']


def test_graph_scope_signature_includes_excluded_function_item_fields():
    from backend.services.creator import api

    original = build_default_requirement_graph(
        [_script_spec(path="scripts/a.py"), _script_spec(path="scripts/b.py")],
        responsibility_edges=[],
        function_items=[
            {"target_file": "scripts/a.py", "purpose": "A", "inputs": ["in"], "outputs": ["mid"], "constraints": []},
            {"target_file": "scripts/b.py", "purpose": "B", "inputs": ["mid"], "outputs": ["out"], "constraints": [{"name": "keep", "kind": "constraint", "value": "v1", "comparator": "describes"}]},
        ],
    )
    refined = build_default_requirement_graph(
        [_script_spec(path="scripts/a.py"), _script_spec(path="scripts/b.py")],
        responsibility_edges=[],
        function_items=[
            {"target_file": "scripts/a.py", "purpose": "A", "inputs": ["in"], "outputs": ["mid"], "constraints": []},
            {"target_file": "scripts/b.py", "purpose": "B", "inputs": ["mid"], "outputs": ["out"], "constraints": [{"name": "keep", "kind": "constraint", "value": "changed", "comparator": "describes"}]},
        ],
    )

    with pytest.raises(ValueError, match="unrelated FunctionItem"):
        api._validate_refined_graph_scope(before=original, after=refined, feedback={"related_nodes": ["scripts/a.py"]})


def test_graph_refine_rejects_unrelated_edge_semantics_change():
    from backend.services.creator import api

    edge = {"from_node": "scripts/a.py", "from_output": "mid", "to_node": "scripts/b.py", "to_input": "mid", "purpose": "handoff", "constraints": [{"name": "c", "value": "v"}]}
    original = build_default_requirement_graph(
        [_script_spec(path="scripts/a.py"), _script_spec(path="scripts/b.py")],
        responsibility_edges=[edge],
        function_items=[
            {"target_file": "scripts/a.py", "purpose": "A", "inputs": ["in"], "outputs": ["mid"], "constraints": []},
            {"target_file": "scripts/b.py", "purpose": "B", "inputs": ["mid"], "outputs": ["out"], "constraints": []},
        ],
    )
    changed = dict(edge, purpose="changed", constraints=[{"name": "c", "value": "changed"}])
    refined = build_default_requirement_graph(
        [_script_spec(path="scripts/a.py"), _script_spec(path="scripts/b.py")],
        responsibility_edges=[changed],
        function_items=[
            {"target_file": "scripts/a.py", "purpose": "A", "inputs": ["in"], "outputs": ["mid"], "constraints": []},
            {"target_file": "scripts/b.py", "purpose": "B", "inputs": ["mid"], "outputs": ["out"], "constraints": []},
        ],
    )

    with pytest.raises(ValueError, match="unrelated responsibility edge"):
        api._validate_refined_graph_scope(before=original, after=refined, feedback={"related_nodes": ["platform_input_node"]})


def test_graph_refine_allows_related_platform_io_edge_change():
    from backend.services.creator import api
    from backend.services.platform_io_contract import build_platform_io_contract

    boundary = build_platform_io_contract()["platform_skill_boundary"]
    original = build_default_requirement_graph(
        [_script_spec(path="scripts/a.py")],
        responsibility_edges=[],
        function_items=[{"target_file": "scripts/a.py", "purpose": "A", "inputs": ["in"], "outputs": ["out"], "constraints": []}],
    )
    refined = build_default_requirement_graph(
        [_script_spec(path="scripts/a.py")],
        responsibility_edges=[{"from_node": "platform_input_node", "from_output": boundary["input_envelope_fields"][0], "to_node": "scripts/a.py", "to_input": "in", "purpose": "provide input", "constraints": []}],
        function_items=[{"target_file": "scripts/a.py", "purpose": "A", "inputs": ["in"], "outputs": ["out"], "constraints": []}],
    )

    api._validate_refined_graph_scope(before=original, after=refined, feedback={"related_nodes": ["platform_input_node", "scripts/a.py"]})


def test_graph_refine_scope_failure_retries_then_succeeds(monkeypatch):
    import asyncio
    from backend.services.creator import api
    from backend.services.platform_io_contract import build_platform_io_contract

    boundary = build_platform_io_contract()["platform_skill_boundary"]
    calls = []

    async def fake_complete(messages, model):
        calls.append(messages)
        if len(calls) == 1:
            return json.dumps({
                "function_items": [{"target_file": "scripts/generic.py", "purpose": "unrelated semantic rewrite", "inputs": ["semantic source"], "outputs": ["semantic artifact"], "constraints": []}],
                "responsibility_edges": [],
            })
        return json.dumps({
            "function_items": [{"target_file": "scripts/generic.py", "purpose": "Transform input.", "inputs": ["semantic source"], "outputs": ["semantic artifact"], "constraints": []}],
            "responsibility_edges": [
                {"from_node": "platform_input_node", "from_output": boundary["input_envelope_fields"][0], "to_node": "scripts/generic.py", "to_input": "semantic source", "purpose": "provide input", "constraints": []},
                {"from_node": "scripts/generic.py", "from_output": "semantic artifact", "to_node": "platform_output_node", "to_input": boundary["final_output_fields"][0], "purpose": "deliver output", "constraints": []},
            ],
        })

    monkeypatch.setattr(api, "complete_chat_once", fake_complete)
    graph = asyncio.run(api._extract_requirement_graph_with_validator(
        blueprint_text="blueprint",
        files_out=[_script_spec()],
        responsibility_edges=[],
        function_items=[{"target_file": "scripts/generic.py", "purpose": "Transform input.", "inputs": ["semantic source"], "outputs": ["semantic artifact"], "constraints": []}],
    ))

    assert len(calls) == 2
    assert graph.dataflow_edges
