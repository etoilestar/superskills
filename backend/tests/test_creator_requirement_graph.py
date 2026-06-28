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
async def test_tool_contract_mismatch_preempts_validator_and_enters_patchable_failure(monkeypatch):
    spec = _script_spec(selected_tools=[], required_capabilities=[])
    req = build_default_requirement_graph([spec]).requirements[0]

    async def fake_complete(*args, **kwargs):
        raise AssertionError("deterministic tool contract should run before validator")

    monkeypatch.setattr("backend.services.creator.repair.complete_chat_once", fake_complete)
    review = await _run_script_responsibility_review(
        file_path=spec.path,
        script_content="from backend.services.runtime_tools import missing_runtime_helper\n\ndef run(payload):\n    return missing_runtime_helper(payload)\n",
        skill_plan_entry=spec,
        requirements=[req],
    )
    assert review["failure_type"] == "script_requirement_failed"
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
async def test_validator_blocking_without_checks_tool_mismatch_becomes_patchable(monkeypatch):
    spec = _script_spec(selected_tools=[], required_capabilities=[])
    req = build_default_requirement_graph([spec]).requirements[0]

    async def fake_complete(*args, **kwargs):
        return '{"passed": false, "blocking_issues": [{"problem": "bad helper"}]}'

    monkeypatch.setattr("backend.services.creator.repair.complete_chat_once", fake_complete)
    review = await _run_script_responsibility_review(
        file_path=spec.path,
        script_content="from backend.services.runtime_tools import missing_runtime_helper\n\ndef run(payload):\n    return missing_runtime_helper(payload)\n",
        skill_plan_entry=spec,
        requirements=[req],
    )
    assert review["failure_type"] == "script_requirement_failed"
    assert review["issues"][0]["id"] == "tool_contract_mismatch"


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
        return '{"passed": true, "advisory_notes": []}'

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
        return '{"passed": true, "advisory_notes": []}'

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
        '{"passed": true, "checks": [{"requirement_id": "' + req.id + '", "status": "passed", "severity": "advisory", "evidence_level": "strong", "missing_evidence": []}], "advisory_notes": []}',
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
    assert review["passed"] is True
    assert review["failure_type"] == "script_requirement_validator_incomplete"
    assert review["issues"] == []


def test_script_requirement_failed_uses_localized_patch_mode():
    from backend.services.creator.api import _repair_mode_for_first_round

    assert _repair_mode_for_first_round(
        source="script_requirement_failed",
        file_path="scripts/generic.py",
        attempt=1,
    ) == "localized_patch"


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
        return '{"passed": true, "advisory_notes": []}'

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


def test_valid_templated_json_argv_does_not_enter_finalize_format_rewrite():
    from backend.services.creator import api
    from backend.services.creator.contracts import _validate_command_is_single_shell_json_invocation
    from backend.services.skill_plan import build_skill_plan_entry

    entry = build_skill_plan_entry(file_path="scripts/main.py", purpose="role: generic_script")
    results = _validate_command_is_single_shell_json_invocation(
        command='python scripts/main.py \'{"image_paths":{{image_paths}}}\'',
        script_path="scripts/main.py",
        entry=entry,
    )
    failed = [
        {
            "id": result.id,
            "layer": result.layer,
            "details": result.details,
        }
        for result in results
        if not result.passed
    ]
    format_rewrite, _content_patch, _deferred = api._split_skill_md_finalize_failures(failed)

    assert not any(result.id == "skill_md.command_block.args_parseable" and not result.passed for result in results)
    assert format_rewrite == []


def test_finalize_split_patches_remaining_hard_failures_without_whitelist():
    from backend.services.creator import api

    failure = {
        "id": "skill_md.custom_content_contract",
        "target": "SKILL.md",
        "layer": "skill_md_first_round",
        "message": "ordinary content contract failed",
    }
    format_rewrite, patchable, deferred = api._split_skill_md_finalize_failures([failure])

    assert format_rewrite == []
    assert patchable == [failure]
    assert deferred == []


@pytest.mark.asyncio
async def test_finalize_skill_md_command_failures_trigger_full_rewrite_not_patch(monkeypatch, tmp_path):
    from backend.config import settings
    from backend.services.creator import api
    from backend.services.creator.common import FinalizeSkillMdRequest

    monkeypatch.setattr(settings, "skills_path", tmp_path)

    generation_calls = []

    async def fake_complete_creator_file_generation(**_kwargs):
        generation_calls.append(_kwargs)
        return "---\nname: demo-skill\ndescription: demo\n---\n# Demo\n"

    async def fail_repair(**_kwargs):
        raise AssertionError("command-like failures must not enter localized patch")

    monkeypatch.setattr(api, "_complete_creator_file_generation", fake_complete_creator_file_generation)
    monkeypatch.setattr(api, "_skill_md_first_round_failures", lambda **_kwargs: [{
        "id": "skill_md.command_block.signature_parseable",
        "target": "scripts/main.py",
        "layer": "skill_md_first_round",
        "message": "command contract failed",
        "minimal_edit": "defer",
    }])
    monkeypatch.setattr(api, "_repair_generated_file_with_feedback", fail_repair)

    result = await api.finalize_skill_md(FinalizeSkillMdRequest(
        skill_name="demo-skill",
        description="demo",
        blueprint_text="demo",
    ))

    assert result["validation_status"] == "needs_repair"
    assert result["editable"] is True
    assert len(generation_calls) > 1
    assert result["repair_events"][0]["patch_status"] == "format_full_rewrite"


@pytest.mark.asyncio
async def test_repair_skill_md_model_finalizer_targets_only_markdown_content(monkeypatch):
    from backend.services.creator import api

    captured = {}

    async def fake_repair_generated_file_with_feedback(**kwargs):
        captured.update(kwargs)
        return kwargs["previous_content"] + "\n补充说明\n"

    monkeypatch.setattr(api, "_repair_generated_file_with_feedback", fake_repair_generated_file_with_feedback)

    await api._repair_skill_md_model_finalizer(
        previous_content="---\nname: demo\ndescription: demo\n---\n# Demo\n",
        failures=[{
            "id": "skill_md.script.narrative_quality",
            "target": "scripts/main.py",
            "layer": "skill_md_first_round",
            "minimal_edit": "补充自然语言职责说明。",
        }],
        prompt_messages=[],
        model="test-model",
        skill_name="demo",
        attempt=1,
    )

    combined = "\n".join(str(captured.get(key) or "") for key in ("validation_error", "targeted_repair", "contract_text"))
    for forbidden in ["command", "argv", "bash", "JSON argv", "fenced", "single_command", "signature_parseable"]:
        assert forbidden not in combined
    assert captured["patch_retry_limit"] == 1


@pytest.mark.asyncio
async def test_finalize_skill_md_reference_mentioned_uses_content_patch(monkeypatch, tmp_path):
    from backend.config import settings
    from backend.services.creator import api
    from backend.services.creator.common import FinalizeSkillMdRequest

    monkeypatch.setattr(settings, "skills_path", tmp_path)
    repair_calls = []

    async def fake_complete_creator_file_generation(**_kwargs):
        return "---\nname: demo-skill\ndescription: demo\n---\n# Demo\n"

    async def fake_repair_generated_file_with_feedback(**kwargs):
        repair_calls.append(kwargs)
        return kwargs["previous_content"] + "\n参考资料说明。\n"

    reviews = iter([
        [{
            "id": "skill_md.reference.mentioned",
            "target": "references/guide.md",
            "layer": "skill_md_first_round",
            "message": "reference is not described",
            "minimal_edit": "add ordinary reference explanation",
        }],
        [],
    ])

    monkeypatch.setattr(api, "_complete_creator_file_generation", fake_complete_creator_file_generation)
    monkeypatch.setattr(api, "_skill_md_first_round_failures", lambda **_kwargs: next(reviews))
    monkeypatch.setattr(api, "_repair_generated_file_with_feedback", fake_repair_generated_file_with_feedback)
    async def noop_blueprint_alignment(**_kwargs):
        return None

    monkeypatch.setattr(api, "_validate_skill_md_blueprint_alignment", noop_blueprint_alignment)

    result = await api.finalize_skill_md(FinalizeSkillMdRequest(
        skill_name="demo-skill",
        description="demo",
        blueprint_text="demo",
    ))

    assert repair_calls
    combined = repair_calls[0]["validation_error"] + repair_calls[0]["targeted_repair"]
    assert "reference is not described" in combined
    for forbidden in ["command", "argv", "bash", "JSON argv"]:
        assert forbidden not in combined
    assert result["validation_status"] == "passed"


@pytest.mark.asyncio
async def test_finalize_skill_md_command_failure_precedes_reference_patch(monkeypatch, tmp_path):
    from backend.config import settings
    from backend.services.creator import api
    from backend.services.creator.common import FinalizeSkillMdRequest

    monkeypatch.setattr(settings, "skills_path", tmp_path)
    generation_calls = []
    repair_calls = []
    failure_rounds = iter([
        [
            {
                "id": "skill_md.command_block.json_argv_object",
                "target": "scripts/main.py",
                "layer": "skill_md_first_round",
                "message": "command shape failed",
                "minimal_edit": "rewrite",
            },
            {
                "id": "skill_md.reference.mentioned",
                "target": "references/guide.md",
                "layer": "skill_md_first_round",
                "message": "reference is not described",
                "minimal_edit": "add ordinary reference explanation",
            },
        ],
        [
            {
                "id": "skill_md.reference.mentioned",
                "target": "references/guide.md",
                "layer": "skill_md_first_round",
                "message": "reference is not described",
                "minimal_edit": "add ordinary reference explanation",
            }
        ],
        [],
    ])

    async def fake_complete_creator_file_generation(**kwargs):
        generation_calls.append(kwargs)
        return "---\nname: demo-skill\ndescription: demo\n---\n# Demo\n"

    async def fake_repair_generated_file_with_feedback(**kwargs):
        repair_calls.append(kwargs)
        return kwargs["previous_content"] + "\n参考资料说明。\n"

    monkeypatch.setattr(api, "_complete_creator_file_generation", fake_complete_creator_file_generation)
    monkeypatch.setattr(api, "_skill_md_first_round_failures", lambda **_kwargs: next(failure_rounds))
    monkeypatch.setattr(api, "_repair_generated_file_with_feedback", fake_repair_generated_file_with_feedback)
    async def noop_blueprint_alignment(**_kwargs):
        return None

    monkeypatch.setattr(api, "_validate_skill_md_blueprint_alignment", noop_blueprint_alignment)

    result = await api.finalize_skill_md(FinalizeSkillMdRequest(
        skill_name="demo-skill",
        description="demo",
        blueprint_text="demo",
    ))

    assert len(generation_calls) == 2
    assert repair_calls
    assert result["repair_events"][0]["patch_status"] == "format_full_rewrite"
    assert "command shape failed" not in repair_calls[0]["validation_error"]
    assert result["validation_status"] == "passed"


@pytest.mark.asyncio
async def test_finalize_skill_md_parse_failed_returns_editable_without_patch_retry(monkeypatch, tmp_path):
    from backend.config import settings
    from backend.services.creator import api
    from backend.services.creator.common import FinalizeSkillMdRequest
    from backend.services.creator.repair import CreatorRepairProposalParseError

    monkeypatch.setattr(settings, "skills_path", tmp_path)
    repair_calls = []

    async def fake_complete_creator_file_generation(**_kwargs):
        return "---\nname: demo-skill\ndescription: demo\n---\n# Demo\nscripts/main.py\n"

    async def fake_repair_generated_file_with_feedback(**_kwargs):
        repair_calls.append(_kwargs)
        raise CreatorRepairProposalParseError("bad proposal", parser_error="bad proposal")

    monkeypatch.setattr(api, "_complete_creator_file_generation", fake_complete_creator_file_generation)
    monkeypatch.setattr(api, "_skill_md_first_round_failures", lambda **_kwargs: [{
        "id": "skill_md.script.narrative_quality",
        "target": "scripts/main.py",
        "layer": "skill_md_first_round",
        "message": "missing natural language responsibility explanation",
        "minimal_edit": "add ordinary explanation",
    }])
    monkeypatch.setattr(api, "_repair_generated_file_with_feedback", fake_repair_generated_file_with_feedback)

    result = await api.finalize_skill_md(FinalizeSkillMdRequest(
        skill_name="demo-skill",
        description="demo",
        blueprint_text="demo",
    ))

    assert len(repair_calls) == 1
    assert result["validation_status"] == "needs_repair"
    assert result["editable"] is True
    assert result["repair_events"][0]["patch_status"] == "parse_failed"


@pytest.mark.asyncio
async def test_finalize_skill_md_patch_failed_returns_editable(monkeypatch, tmp_path):
    from backend.config import settings
    from backend.services.creator import api
    from backend.services.creator.common import FinalizeSkillMdRequest

    monkeypatch.setattr(settings, "skills_path", tmp_path)
    repair_calls = []

    async def fake_complete_creator_file_generation(**_kwargs):
        return "---\nname: demo-skill\ndescription: demo\n---\n# Demo\nscripts/main.py\n"

    async def fake_repair_generated_file_with_feedback(**kwargs):
        repair_calls.append(kwargs)
        raise RuntimeError("patch apply failed")

    monkeypatch.setattr(api, "_complete_creator_file_generation", fake_complete_creator_file_generation)
    monkeypatch.setattr(api, "_skill_md_first_round_failures", lambda **_kwargs: [{
        "id": "skill_md.custom_content_contract",
        "target": "SKILL.md",
        "layer": "skill_md_first_round",
        "message": "ordinary content contract failed",
        "minimal_edit": "repair ordinary content",
    }])
    monkeypatch.setattr(api, "_repair_generated_file_with_feedback", fake_repair_generated_file_with_feedback)

    result = await api.finalize_skill_md(FinalizeSkillMdRequest(
        skill_name="demo-skill",
        description="demo",
        blueprint_text="demo",
    ))

    assert len(repair_calls) == 1
    assert result["validation_status"] == "needs_repair"
    assert result["editable"] is True
    assert result["repair_events"][0]["patch_status"] == "patch_failed"


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

    seen_static_requirements = []

    def fake_static_blockers(script_content, skill_plan_entry, requirements):
        seen_static_requirements.extend(requirements or [])
        if requirements:
            return [{
                "id": "script_requirement_failed",
                "requirement_id": req.id,
                "failed_file": "scripts/main.py",
                "reason": "required input is not in the constructed output",
                "missing_evidence": ["required input -> output"],
                "minimal_edit": "patch current script",
                "allowed_scope": "current script only",
            }]
        return []

    repair_modes = []

    async def fake_repair_generated_file_with_feedback(**kwargs):
        repair_modes.append(kwargs.get("repair_mode"))
        return "def run(payload):\n    brief = payload.get('customer brief')\n    return {'path': brief}\n"

    monkeypatch.setattr(api, "_complete_creator_file_generation", fake_complete_creator_file_generation)
    monkeypatch.setattr(api, "_run_script_responsibility_review", fake_responsibility_review)
    monkeypatch.setattr(api, "_load_persisted_requirement_graph", lambda _skill_name: None)
    monkeypatch.setattr(api, "_detect_script_responsibility_static_blockers", fake_static_blockers)
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

    assert seen_static_requirements, body
    assert repair_modes, body
    assert "script_requirement_failed" in body
    assert "script_requirement_validator_incomplete" not in body


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

def test_validator_error_stage_enters_repair_instead_of_frontend_error():
    from backend.services.creator import api

    # Contract-level assertion: single-file production validator failures are
    # converted into current-file repair issues instead of terminal SSE errors.
    import inspect
    source = inspect.getsource(api.generate_file)
    assert "script_requirement_validator_error" in source
    assert "single_file_production_validation" in source
    assert "auto-repair the current file instead of returning directly to the frontend" in source
    assert "no localized business blocker was identified" not in source


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
