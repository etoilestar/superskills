import hashlib
import inspect
import json

import pytest

from backend.services.creator_contracts import (
    compile_canonical_file_contract,
    resolve_implementation,
    validate_python_evidence,
)
from backend.services.skill_plan import SkillPlanEntry, ToolSlot


def _entry(**kw):
    data = dict(
        path="scripts/main.py",
        role="generic_script",
        file_type="script",
        purpose="test",
        runtime="python",
        inputs=["payload"],
        outputs=["result"],
        dependencies=[],
        required_capabilities=[],
    )
    data.update(kw)
    return SkillPlanEntry(**data)


def _schema():
    return {"type": "object", "required": ["result"], "properties": {"result": {"type": "string"}}}


def test_script_composition_rejects_literal_shell_script():
    entry = _entry()
    contract = compile_canonical_file_contract(entry, _schema())
    resolution = resolve_implementation(entry, contract)

    code = """
def run(payload):
    return {"result": "fixed"}
"""

    issues = validate_python_evidence(code, contract, resolution)

    assert resolution.mode == "script_composition"
    assert any(issue.startswith("input_dependency") for issue in issues)
    assert any(issue.startswith("nontrivial_transform") for issue in issues)


def test_script_composition_accepts_input_dependent_transform():
    entry = _entry()
    contract = compile_canonical_file_contract(entry, _schema())
    resolution = resolve_implementation(entry, contract)

    code = """
import json

def run(payload):
    text = json.dumps(payload.get("payload", ""), ensure_ascii=False).strip()
    return {"result": text}
"""

    assert validate_python_evidence(code, contract, resolution) == []


def test_available_tool_is_not_forced_but_used_tool_is_validated():
    entry = _entry(role="pdf_builder", required_capabilities=["pdf_generation"], outputs=["pdf_path", "file_outputs"])
    schema = {"type": "object", "required": ["pdf_path", "file_outputs"], "properties": {}}
    contract = compile_canonical_file_contract(entry, schema)
    resolution = resolve_implementation(entry, contract)

    assert resolution.mode == "script_composition"
    no_tool_code = "def run(payload):\n    value = str(payload.get('payload', ''))\n    return {'pdf_path': value + '.pdf', 'file_outputs': [value]}\n"
    assert not any(issue.startswith("tool_call") for issue in validate_python_evidence(no_tool_code, contract, resolution))

    imported_not_called = "from backend.services.runtime_tools import create_pdf\ndef run(payload):\n    value = str(payload.get('payload', ''))\n    return {'pdf_path': value, 'file_outputs': [value]}\n"
    assert any(issue.startswith("tool_call") for issue in validate_python_evidence(imported_not_called, contract, resolution))

    ok_code = """
from backend.services.runtime_tools import create_pdf

def run(payload):
    result = create_pdf(text=str(payload.get("payload", "")))
    return {"pdf_path": result["pdf_path"], "file_outputs": result.get("file_outputs")}
"""
    assert validate_python_evidence(ok_code, contract, resolution) == []


def test_undeclared_third_party_import_fails():
    entry = _entry()
    contract = compile_canonical_file_contract(entry, _schema())
    resolution = resolve_implementation(entry, contract)

    code = """
import requests

def run(payload):
    value = str(payload.get("payload", "")).strip()
    return {"result": value}
"""

    assert any(issue.startswith("declared_dependency_only") for issue in validate_python_evidence(code, contract, resolution))


def test_configured_discovery_adapter_loads_callable_manifests():
    from backend.services.creator_tool_discovery import discover_creator_tool_records

    records = discover_creator_tool_records({"registries": ["backend/config/tool_registry.custom.json"], "modules": []})

    assert records
    assert all(record.get("functions") for record in records)
    assert all("input_schema" in record and "output_schema" in record for record in records)
    assert all("artifact_outputs" in record and "side_effects" in record for record in records)


def test_available_tool_schema_does_not_replace_canonical_contract():
    from backend.services.creator_contracts import refine_contract_with_resolution

    entry = _entry(role="pdf_builder", required_capabilities=["pdf_generation"], outputs=["pdf_path"])
    contract = compile_canonical_file_contract(entry, {"type": "object", "required": ["pdf_path"], "properties": {"pdf_path": {}}})
    resolution = resolve_implementation(entry, contract)
    refined = refine_contract_with_resolution(contract, resolution)

    assert resolution.mode == "script_composition"
    assert "pdf_path" in refined.outputs
    assert refined.stdout_schema["required"] == ["pdf_path"]


def test_references_and_assets_are_not_script_io_keys():
    entry = _entry(
        inputs=["payload", "references/guide.md", "assets/source.png"],
        outputs=["result", "assets/generated/out.png"],
        dependencies=["requests", "references/guide.md", "assets/source.png"],
    )
    contract = compile_canonical_file_contract(entry, _schema())

    assert contract.inputs == ["payload"]
    assert contract.outputs == ["result"]
    assert contract.declared_dependencies == ["requests"]


def test_system_manifest_selects_text_generation_tool():
    entry = _entry(role="text_generator", required_capabilities=["text_generation"], outputs=["text"])
    contract = compile_canonical_file_contract(entry, {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}})
    resolution = resolve_implementation(entry, contract)

    assert resolution.mode == "script_composition"
    assert any(tool.function_name == "generate_text_with_llm" for tool in resolution.available_tools)


def test_system_manifest_selects_image_generation_artifact_tool():
    from backend.services.creator_contracts import refine_contract_with_resolution

    entry = _entry(role="image_generator", required_capabilities=["image_generation"], outputs=["image_path"])
    contract = compile_canonical_file_contract(entry, {"type": "object", "required": ["image_path"], "properties": {"image_path": {"type": "string"}}})
    resolution = resolve_implementation(entry, contract)
    refined = refine_contract_with_resolution(contract, resolution)

    assert resolution.mode == "script_composition"
    assert any(tool.function_name == "generate_stable_diffusion_image" for tool in resolution.available_tools)
    assert "artifact_created" in resolution.required_evidence
    assert not refined.artifact_contract.get("tool_artifact_outputs")


def test_raw_capability_hints_are_candidate_signals_for_tool_resolution():
    entry = _entry(role="text_generator", required_capabilities=[], raw_capability_hints=["text_generation"], outputs=["text"])
    contract = compile_canonical_file_contract(entry, {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}})
    resolution = resolve_implementation(entry, contract)

    assert any(req.capability_id == "text_generation" and req.source == "raw_capability_hints" and not req.required for req in contract.capability_requirements)
    assert resolution.mode == "script_composition"
    assert any(tool.function_name == "generate_text_with_llm" for tool in resolution.available_tools)


def test_single_text_output_mapping_allows_business_stdout_field():
    from backend.services.creator_contracts import refine_contract_with_resolution

    entry = _entry(role="text_generator", required_capabilities=[], raw_capability_hints=["text_generation"], outputs=["article_body"])
    schema = {"type": "object", "required": ["article_body"], "properties": {"article_body": {"type": "string"}}}
    contract = compile_canonical_file_contract(entry, schema)
    resolution = resolve_implementation(entry, contract)
    refined = refine_contract_with_resolution(contract, resolution)

    assert resolution.mode == "script_composition"
    assert resolution.output_mappings == [{"source_tool_field": "text", "target_stdout_field": "article_body"}]
    assert refined.stdout_schema["required"] == ["article_body"]
    assert "text" not in refined.outputs


def test_trial_stdout_keeps_canonical_required_fields():
    from backend.routers.creator import _validate_trial_stdout_json
    from backend.services.creator_contracts import refine_contract_with_resolution

    entry = _entry(role="pdf_builder", required_capabilities=["pdf_generation"], outputs=["pdf_path"])
    contract = compile_canonical_file_contract(entry, {"type": "object", "required": ["pdf_path"], "properties": {"pdf_path": {"type": "string"}}})
    resolution = resolve_implementation(entry, contract)
    refined = refine_contract_with_resolution(contract, resolution)

    assert refined.stdout_schema["required"] == ["pdf_path"]
    _validate_trial_stdout_json(stdout='{"pdf_path":"outputs/a.pdf"}', content="", args=["{}"], canonical_contract=refined)


def test_partial_tool_schema_becomes_available_tool_candidate():
    from backend.services.creator_tool_registry import ToolCapability, ToolFunctionManifest, clear_registered_tool_capabilities, register_tool_capability

    clear_registered_tool_capabilities()
    register_tool_capability(ToolCapability(
        name="lookup_helper",
        display_name="Lookup Helper",
        category="retrieval",
        roles=["generic_script"],
        functions=[ToolFunctionManifest(
            function_name="lookup_value",
            import_path="backend.services.runtime_tools",
            short_description="Lookup one source value.",
            when_to_use="Use for the lookup functional step.",
            signature="lookup_value(query: str) -> dict",
            input_schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
            output_schema={"type": "object", "required": ["source_value"], "properties": {"source_value": {"type": "string"}}},
            required_capabilities=["lookup_helper"],
        )],
    ))
    try:
        entry = _entry(
            raw_capability_hints=["lookup_helper"],
            outputs=["final_answer", "confidence"],
            required_tool_slots=[ToolSlot(slot_id="lookup", functional_requirement="lookup source data")],
        )
        schema = {"type": "object", "required": ["final_answer", "confidence"], "properties": {"final_answer": {"type": "string"}, "confidence": {"type": "number"}}}
        contract = compile_canonical_file_contract(entry, schema)
        resolution = resolve_implementation(entry, contract)

        assert contract.functional_requirements == ["lookup source data"]
        assert resolution.mode == "script_composition"
        assert resolution.available_tools[0].tool_id == "lookup_helper.lookup_value"
        assert resolution.tool_slots[0]["tool_id"] == "lookup_helper.lookup_value"
        assert "tool_result_used" not in resolution.required_evidence
        assert resolution.selected_tools == []
        assert resolution.allowed_imports == ["backend.services.runtime_tools", "backend.services.runtime_tools.lookup_value"]
    finally:
        clear_registered_tool_capabilities()


def test_function_level_tool_imports_share_dependency_validation():
    from backend.services.creator_tool_registry import ToolCapability, ToolFunctionManifest, clear_registered_tool_capabilities, register_tool_capability

    clear_registered_tool_capabilities()
    register_tool_capability(ToolCapability(
        name="custom_lookup",
        display_name="Custom Lookup",
        category="retrieval",
        roles=["generic_script"],
        dependencies=[{"package": "rich", "imports": ["rich"]}, "assets/not_a_dependency.txt", "references/guide.md"],
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
        entry = _entry(raw_capability_hints=["custom_lookup"])
        contract = compile_canonical_file_contract(entry, _schema())
        resolution = resolve_implementation(entry, contract)

        assert "backend.services.runtime_tools.custom_tools.lookup" in resolution.allowed_imports
        assert "backend.services.runtime_tools.custom_tools.lookup.lookup_value" in resolution.allowed_imports
        assert "rich" in resolution.declared_dependencies
        assert "assets/not_a_dependency.txt" not in resolution.declared_dependencies
        assert "references/guide.md" not in resolution.declared_dependencies

        ok_code = """
from backend.services.runtime_tools.custom_tools.lookup import lookup_value

def run(payload):
    value = lookup_value(str(payload.get("payload", "")))
    return {"result": value.get("source_value", "")}
"""
        assert not any(issue.startswith("declared_dependency_only") for issue in validate_python_evidence(ok_code, contract, resolution))

        bad_code = """
from backend.services.runtime_tools.custom_tools.other import missing_lookup

def run(payload):
    value = str(payload.get("payload", "")).strip()
    return {"result": value}
"""
        assert any(issue.startswith("declared_dependency_only") for issue in validate_python_evidence(bad_code, contract, resolution))
    finally:
        clear_registered_tool_capabilities()

from backend.services.creator.contracts import detect_markdown_hard_format_failures, _command_signature, split_markdown_regions, merge_markdown_regions, markdown_failure_region


def test_hard_format_skill_missing_frontmatter_region_rewrite():
    failures = detect_markdown_hard_format_failures("SKILL.md", "# Body\n", True)
    assert failures
    assert failures[0]["severity"] == "hard_format"
    assert failures[0]["details"]["region"] == "metadata_region"
    assert any(f["id"] == "markdown.frontmatter.missing" for f in failures)


def test_hard_format_frontmatter_unclosed_region_rewrite():
    failures = detect_markdown_hard_format_failures("SKILL.md", "---\nname: x\ndescription: y\n# swallowed\n", True)
    assert any(f["id"] == "markdown.frontmatter.unclosed" for f in failures)


def test_hard_format_fenced_block_unclosed_region_rewrite():
    content = "---\nname: x\ndescription: y\n---\n\n```bash\npython scripts/a.py '{}'\n"
    failures = detect_markdown_hard_format_failures("SKILL.md", content, True)
    assert any(f["id"] == "markdown.fences.bash_unclosed" for f in failures)


def test_hard_format_no_body_region_rewrite():
    failures = detect_markdown_hard_format_failures("SKILL.md", "---\nname: x\ndescription: y\n---\n", True)
    assert any(f["id"] == "markdown.body.missing" for f in failures)


def test_markdown_region_split_and_merge_preserves_body_when_metadata_changes():
    content = "---\nname: old\ndescription: old\n---\n\n# Body\n\n```bash\npython scripts/a.py '{} ' \n```\n"
    regions = split_markdown_regions(content)
    updated = merge_markdown_regions("---\nname: new\ndescription: new\n---\n", regions.body_region)
    assert "name: new" in updated
    assert "# Body" in updated
    assert "```bash" in updated


def test_markdown_body_region_rewrite_preserves_metadata_non_regression():
    from backend.services.creator import api

    content = "---\nname: stable\ndescription: keep me\n---\n\n# Old\n"
    updated = api._merge_markdown_region_rewrite(content, "# New\n\n```bash\npython scripts/a.py '{}'\n```\n", "body_region")
    assert updated.startswith("---\nname: stable\ndescription: keep me\n---")
    assert "# New" in updated


def test_markdown_metadata_region_rewrite_preserves_body_non_regression():
    from backend.services.creator import api

    content = "---\nname: old\ndescription: old\n---\n\n# Body\n\nDetails.\n"
    updated = api._merge_markdown_region_rewrite(content, "---\nname: new\ndescription: new\n---\n", "metadata_region")
    assert "name: new" in updated
    assert "# Body\n\nDetails." in updated


def test_markdown_failure_region_routes_metadata_and_body_failures():
    assert markdown_failure_region({"id": "markdown.frontmatter.invalid_yaml"}) == "metadata_region"
    assert markdown_failure_region({"id": "markdown.fences.bash_unclosed"}) == "body_region"


def test_reference_without_frontmatter_allowed_by_hard_gate():
    assert detect_markdown_hard_format_failures("references/guide.md", "# Guide\n\nText.\n", False) == []


def test_reference_unclosed_frontmatter_requires_region_rewrite():
    failures = detect_markdown_hard_format_failures("references/guide.md", "---\ntitle: Guide\n# Body\n", False)
    assert any(f["id"] == "markdown.frontmatter.unclosed" for f in failures)


def test_closed_bash_block_bad_json_argv_is_not_hard_format():
    content = "---\nname: x\ndescription: y\n---\n\n```bash\npython scripts/a.py '{bad}'\n```\n"
    assert detect_markdown_hard_format_failures("SKILL.md", content, True) == []
    sig = _command_signature("python scripts/a.py '{bad}'", "scripts/a.py")
    assert sig is not None
    assert sig["arg_mode"] == "invalid_json_arg"


def test_hard_format_entire_file_fenced_without_language_region_rewrite():
    failures = detect_markdown_hard_format_failures("SKILL.md", "```\n---\nname: x\ndescription: y\n---\n# Body\n```\n", True)
    assert any(f["id"] == "markdown.file.wrapped_in_code_fence" for f in failures)


def test_hard_format_local_plain_fence_is_allowed_when_closed():
    content = "---\nname: x\ndescription: y\n---\n\n# Body\n\n```\nexample\n```\n"
    assert detect_markdown_hard_format_failures("SKILL.md", content, True) == []


def test_blueprint_review_top_level_issues_dedupes_and_ignores_reviewer_duplicates():
    from backend.services.creator.contracts import _skill_md_blueprint_review_to_contract_results

    review = {
        "passed": False,
        "issues": [
            {"severity": "error", "blocking": True, "field": "file_plan", "message": "missing script path", "evidence": "scripts/a.py"},
            {"severity": "error", "blocking": True, "field": "file_plan", "message": "missing script path", "evidence": "scripts/a.py"},
        ],
        "reviewers": {"file_plan_reviewer": {"passed": False, "issues": [{"severity": "error", "field": "file_plan", "message": "duplicate nested", "evidence": "scripts/a.py"}]}},
    }
    results = _skill_md_blueprint_review_to_contract_results(review)
    assert len(results) == 1
    assert "missing script path" in results[0].message


def test_blueprint_wording_advisory_does_not_block():
    from backend.services.creator.contracts import _skill_md_blueprint_review_to_contract_results

    review = {"passed": False, "issues": [{"severity": "warning", "field": "wording", "message": "not detailed enough", "evidence": "summary"}]}
    assert _skill_md_blueprint_review_to_contract_results(review) == []


def test_user_key_requirement_missing_blocks():
    from backend.services.creator.contracts import _skill_md_blueprint_review_to_contract_results

    review = {"passed": False, "issues": [{"severity": "error", "field": "user_requirement", "message": "page count cannot be passed to script", "evidence": "schema lacks input", "contract_impact": {"user_requirement_transfer": True}}]}
    results = _skill_md_blueprint_review_to_contract_results(review)
    assert results and results[0].layer == "skill_md_blueprint_alignment"


def test_reviewer_json_parse_failed_is_validator_error_not_skill_repair():
    from backend.services.creator.api import _exception_to_skill_md_failures, normalize_skill_md_failures
    from backend.services.creator.contracts import CreatorValidatorReviewError

    failures = _exception_to_skill_md_failures(CreatorValidatorReviewError("bad json", raw_excerpt="oops"), source="blueprint_alignment")
    assert failures[0]["layer"] == "validator_error"
    assert normalize_skill_md_failures(failures) == []


def test_plain_error_without_contract_facts_is_advisory():
    from backend.services.creator.contracts import _skill_md_blueprint_review_to_contract_results

    review = {"passed": False, "issues": [{"severity": "error", "field": "user_facing", "message": "description could be richer"}]}
    assert _skill_md_blueprint_review_to_contract_results(review) == []


def test_explicit_blocking_false_does_not_block():
    from backend.services.creator.contracts import _skill_md_blueprint_review_to_contract_results

    review = {"passed": False, "issues": [{"severity": "error", "blocking": False, "field": "workflow", "message": "role tag absent", "contract_impact": {"execution_closure": True}}]}
    assert _skill_md_blueprint_review_to_contract_results(review) == []


def test_reviewer_dedupe_ignores_changing_evidence():
    from backend.services.creator.contracts import _dedupe_review_issues

    issues = [
        {"severity": "error", "field": "file_plan", "message": "same", "expected": "same expected", "evidence": "old evidence"},
        {"severity": "error", "field": "file_plan", "message": "same", "expected": "same expected", "evidence": "new evidence"},
    ]
    assert len(_dedupe_review_issues(issues)) == 1


@pytest.mark.asyncio
async def test_skill_md_reviewer_invalid_json_retries_and_keeps_candidate(monkeypatch):
    from backend.services.creator import contracts

    calls = []

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(contracts, "route_model", lambda *a, **k: Route())

    async def fake_complete(messages, model):
        calls.append(messages)
        if len(calls) == 1:
            return "not json"
        assert "只做 JSON schema 格式重写" in messages[-1]["content"]
        return json.dumps({"passed": True, "issues": []})

    monkeypatch.setattr(contracts, "complete_chat_once", fake_complete)
    result = await contracts._review_skill_md_blueprint_intent_with_model(
        skill_name="demo",
        content="original SKILL.md candidate",
        blueprint_text="blueprint",
        skill_plan_entry={},
    )
    assert result["passed"] is True
    assert len(calls) == 2
    assert "original SKILL.md candidate" in calls[1][1]["content"]


@pytest.mark.asyncio
async def test_skill_md_single_command_block_repair_only_replaces_failed_block(monkeypatch):
    from backend.services.creator import api

    first_block = "```bash\npython scripts/one.py '{\"a\":\"{{user_request}}\"}'\n```\n"
    bad_block = "```bash\npython scripts/two.py '{\"bad\":\"literal\"}'\n```\n"
    third_block = "```bash\npython scripts/three.py '{\"c\":\"{{two}}\"}'\n```\n"
    frontmatter = "---\nname: demo\ndescription: demo\n---\n"
    body_before = "# Demo\n\nRun one:\n"
    middle = "\nRun two:\n"
    body_after = "\nRun three:\n"
    tail = "\nDone.\n"
    candidate = frontmatter + body_before + first_block + middle + bad_block + body_after + third_block + tail
    start = candidate.index(bad_block)
    end = start + len(bad_block)
    locator = {
        "block_text": bad_block,
        "script_path": "scripts/two.py",
        "block_start": start,
        "block_end": end,
        "block_sha256": api.hashlib.sha256(bad_block.encode("utf-8")).hexdigest(),
        "structured_checks": {"value_checks": [{"passed": False}]},
        "failure_reasons": [{"message": "literal value has no source"}],
    }
    repaired_block = "```bash\npython scripts/two.py '{\"text\":\"{{one}}\"}'\n```\n"
    seen_prompts = []

    async def fake_complete(**kwargs):
        seen_prompts.extend(kwargs["messages"])
        return repaired_block

    monkeypatch.setattr(api, "_complete_creator_file_generation", fake_complete)
    block = await api._repair_skill_md_command_block(
        model="unit-test",
        skill_name="demo",
        block_text=bad_block,
        script_path="scripts/two.py",
        structured_checks=locator["structured_checks"],
        failure_reasons=locator["failure_reasons"],
    )
    repaired = api._replace_skill_md_command_block_exact(candidate, locator, block)

    assert repaired[start:start + len(repaired_block)] == repaired_block
    assert repaired[:start] == candidate[:start]
    assert repaired[start + len(repaired_block):] == candidate[end:]
    assert first_block in repaired
    assert third_block in repaired
    assert frontmatter in repaired
    assert body_before in repaired and middle in repaired and body_after in repaired and tail in repaired
    assert all(candidate not in message["content"] for message in seen_prompts)


@pytest.mark.asyncio
async def test_skill_md_single_command_block_repair_rejects_full_document(monkeypatch):
    from backend.services.creator import api

    bad_block = "```bash\npython scripts/two.py '{\"bad\":\"literal\"}'\n```\n"
    candidate = "---\nname: demo\ndescription: demo\n---\n# Demo\n" + bad_block
    locator = {
        "block_text": bad_block,
        "script_path": "scripts/two.py",
        "block_start": candidate.index(bad_block),
        "block_end": candidate.index(bad_block) + len(bad_block),
        "block_sha256": api.hashlib.sha256(bad_block.encode("utf-8")).hexdigest(),
        "structured_checks": {},
        "failure_reasons": [{"message": "bad block"}],
    }
    full_document = "---\nname: demo\ndescription: demo\n---\n# Demo\n```bash\npython scripts/two.py '{}'\n```\n"

    async def fake_complete(**kwargs):
        return full_document

    async def forbidden_whole_file_repair(**kwargs):
        raise AssertionError("whole-file repair must not be called")

    monkeypatch.setattr(api, "_complete_creator_file_generation", fake_complete)
    monkeypatch.setattr(api, "_repair_generated_file_with_feedback", forbidden_whole_file_repair)

    with pytest.raises(ValueError):
        block = await api._repair_skill_md_command_block(
            model="unit-test",
            skill_name="demo",
            block_text=bad_block,
            script_path="scripts/two.py",
            structured_checks=locator["structured_checks"],
            failure_reasons=locator["failure_reasons"],
        )
        api._replace_skill_md_command_block_exact(candidate, locator, block)

    assert candidate == "---\nname: demo\ndescription: demo\n---\n# Demo\n" + bad_block


@pytest.mark.asyncio
async def test_skill_md_reviewer_invalid_json_then_passed_false_enters_semantic_failure(monkeypatch):
    from backend.services.creator import contracts

    calls = []

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(contracts, "route_model", lambda *a, **k: Route())

    async def fake_complete(messages, model):
        calls.append(messages)
        if len(calls) == 1:
            return "not json"
        return json.dumps({
            "passed": False,
            "issues": [{
                "severity": "error",
                "blocking": True,
                "field": "workflow",
                "message": "missing real script path",
                "expected": "mention scripts/run.py",
                "contract_impact": {"execution_closure": True},
            }],
            "repair_suggestions": "patch workflow only",
        })

    monkeypatch.setattr(contracts, "complete_chat_once", fake_complete)
    result = await contracts._review_skill_md_blueprint_intent_with_model(
        skill_name="demo",
        content="candidate",
        blueprint_text="blueprint",
        skill_plan_entry={},
    )
    assert result["passed"] is False
    assert result["issues"][0]["message"] == "missing real script path"


@pytest.mark.asyncio
async def test_skill_md_reviewer_three_invalid_json_raises_validator_error(monkeypatch):
    from backend.services.creator import contracts
    from backend.services.creator.contracts import CreatorValidatorReviewError

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(contracts, "route_model", lambda *a, **k: Route())
    calls = {"count": 0}

    async def fake_complete(messages, model):
        calls["count"] += 1
        return "not json"

    monkeypatch.setattr(contracts, "complete_chat_once", fake_complete)
    with pytest.raises(CreatorValidatorReviewError):
        await contracts._review_skill_md_blueprint_intent_with_model(
            skill_name="demo",
            content="candidate",
            blueprint_text="blueprint",
            skill_plan_entry={},
        )
    assert calls["count"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"passed": "false"},
    {"foo": "bar"},
    {"passed": True, "issues": "bad"},
])
async def test_skill_md_reviewer_schema_invalid_never_passes(monkeypatch, payload):
    from backend.services.creator import contracts
    from backend.services.creator.contracts import CreatorValidatorReviewError

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(contracts, "route_model", lambda *a, **k: Route())

    async def fake_complete(messages, model):
        return json.dumps(payload)

    monkeypatch.setattr(contracts, "complete_chat_once", fake_complete)
    with pytest.raises(CreatorValidatorReviewError):
        await contracts._review_skill_md_blueprint_intent_with_model(
            skill_name="demo",
            content="candidate",
            blueprint_text="blueprint",
            skill_plan_entry={},
        )


@pytest.mark.asyncio
async def test_skill_md_reviewer_invalid_schema_then_valid_pass(monkeypatch):
    from backend.services.creator import contracts

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(contracts, "route_model", lambda *a, **k: Route())
    calls = iter([json.dumps({"passed": "false"}), json.dumps({"passed": True, "issues": []})])

    async def fake_complete(messages, model):
        return next(calls)

    monkeypatch.setattr(contracts, "complete_chat_once", fake_complete)
    result = await contracts._review_skill_md_blueprint_intent_with_model(
        skill_name="demo",
        content="candidate",
        blueprint_text="blueprint",
        skill_plan_entry={},
    )
    assert result["passed"] is True


@pytest.mark.asyncio
async def test_skill_md_reviewer_invalid_schema_then_valid_semantic_fail(monkeypatch):
    from backend.services.creator import contracts

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(contracts, "route_model", lambda *a, **k: Route())
    calls = iter([
        json.dumps({"passed": True, "issues": "bad"}),
        json.dumps({
            "passed": False,
            "issues": [{
                "severity": "error",
                "blocking": True,
                "field": "workflow",
                "message": "missing real script path",
                "expected": "mention scripts/run.py",
                "contract_impact": {"execution_closure": True},
            }],
            "repair_suggestions": "patch workflow only",
        }),
    ])

    async def fake_complete(messages, model):
        return next(calls)

    monkeypatch.setattr(contracts, "complete_chat_once", fake_complete)
    result = await contracts._review_skill_md_blueprint_intent_with_model(
        skill_name="demo",
        content="candidate",
        blueprint_text="blueprint",
        skill_plan_entry={},
    )
    assert result["passed"] is False
    assert result["issues"][0]["message"] == "missing real script path"


@pytest.mark.asyncio
async def test_skill_md_reviewer_three_invalid_schema_raises_validator_error(monkeypatch):
    from backend.services.creator import contracts
    from backend.services.creator.contracts import CreatorValidatorReviewError

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(contracts, "route_model", lambda *a, **k: Route())
    calls = {"count": 0}

    async def fake_complete(messages, model):
        calls["count"] += 1
        return json.dumps({"passed": True, "issues": "bad"})

    monkeypatch.setattr(contracts, "complete_chat_once", fake_complete)
    with pytest.raises(CreatorValidatorReviewError):
        await contracts._review_skill_md_blueprint_intent_with_model(
            skill_name="demo",
            content="candidate",
            blueprint_text="blueprint",
            skill_plan_entry={},
        )
    assert calls["count"] == 3


def test_command_template_source_proof_blocks_only_platform_io():
    from backend.services.creator.contracts import _skill_md_blueprint_review_to_contract_results

    internal_review = {
        "passed": False,
        "issues": [{
            "severity": "error",
            "field": "workflow",
            "category": "command_template_source_proof",
            "message": "internal stdout placeholder source not proven",
            "contract_impact": {"execution_closure": True, "platform_io": False},
        }],
    }
    assert _skill_md_blueprint_review_to_contract_results(internal_review) == []

    platform_review = {
        "passed": False,
        "issues": [{
            "severity": "error",
            "field": "workflow",
            "category": "command_template_source_proof",
            "message": "platform input envelope to first command is fixed literal",
            "contract_impact": {"execution_closure": True, "platform_io": True},
        }],
    }
    assert _skill_md_blueprint_review_to_contract_results(platform_review) == []


@pytest.mark.parametrize("payload", [
    {"passed": True, "issues": [{"severity": "error", "blocking": True}]},
    {"passed": True, "reviewers": {"workflow_reviewer": {"passed": False, "issues": []}}, "issues": []},
    {"passed": True, "reviewers": {"workflow_reviewer": {"passed": True, "issues": [{"severity": "error", "blocking": True}]}}},
])
def test_skill_md_reviewer_passed_true_protocol_contradictions_are_schema_errors(payload):
    from backend.services.creator.contracts import _skill_md_reviewer_schema_error

    assert "protocol contradiction" in _skill_md_reviewer_schema_error(payload)


def test_skill_md_reviewer_passed_true_warning_blocking_false_is_allowed():
    from backend.services.creator.contracts import _skill_md_reviewer_schema_error

    payload = {"passed": True, "issues": [{"severity": "warning", "blocking": False}], "reviewers": {"workflow_reviewer": {"passed": True, "issues": [{"severity": "warning", "blocking": False}]}}}
    assert _skill_md_reviewer_schema_error(payload) == ""


@pytest.mark.asyncio
async def test_skill_md_reviewer_three_protocol_contradictions_raise_validator_error(monkeypatch):
    from backend.services.creator import contracts
    from backend.services.creator.contracts import CreatorValidatorReviewError

    class Route:
        model = "unit-test-model"

    monkeypatch.setattr(contracts, "route_model", lambda *a, **k: Route())
    calls = {"count": 0}

    async def fake_complete(messages, model):
        calls["count"] += 1
        return json.dumps({"passed": True, "issues": [{"severity": "error", "blocking": True}]})

    monkeypatch.setattr(contracts, "complete_chat_once", fake_complete)
    with pytest.raises(CreatorValidatorReviewError):
        await contracts._review_skill_md_blueprint_intent_with_model(
            skill_name="demo",
            content="candidate",
            blueprint_text="blueprint",
            skill_plan_entry={},
        )
    assert calls["count"] == 3

@pytest.mark.asyncio
async def test_skill_md_reviewer_prompt_includes_script_interface_and_incoming_edges(monkeypatch, tmp_path):
    from backend.services.creator import contracts

    skill_dir = tmp_path / "demo"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "main.py").write_text(
        """
ALLOWED_KEYS = {"source_text", "style"}
REQUIRED_KEYS = {"source_text"}
OPTIONAL_KEYS = {"style"}

def run(args):
    text = args["source_text"]
    style = args.get("style", "plain")
    return {"normalized_text": f"{style}:{text}"}
""".strip(),
        encoding="utf-8",
    )

    class Route:
        model = "unit-test-model"

    captured = {}
    monkeypatch.setattr(contracts.settings, "skills_path", tmp_path)
    monkeypatch.setattr(contracts, "route_model", lambda *a, **k: Route())

    async def fake_complete(messages, model):
        captured["prompt"] = messages[1]["content"]
        return json.dumps({"passed": True, "issues": []})

    monkeypatch.setattr(contracts, "complete_chat_once", fake_complete)

    result = await contracts._review_skill_md_blueprint_intent_with_model(
        skill_name="demo",
        content="""---
name: demo
description: demo
---
Run:
```bash
python scripts/main.py '{"source_text":"${user_text}","style":"plain"}'
```
""",
        blueprint_text="""用户输入 user_text，经真实规划脚本输出 normalized_text。
""",
        skill_plan_entry={"files": [{"path": "scripts/main.py", "file_type": "script"}]},
        requirement_graph={
            "requirements": [
                {
                    "target_file": "scripts/main.py",
                    "role": "processor",
                    "runtime": "python",
                    "purpose": "Normalize user text.",
                    "inputs": ["source_text"],
                    "outputs": ["normalized_text"],
                }
            ],
            "platform_input_node": {
                "node_id": "platform_input_node",
                "node_type": "platform_input",
                "outputs": ["user_text"],
            },
            "dataflow_edges": [
                {
                    "from_node": "platform_input_node",
                    "from_output": "user_text",
                    "to_node": "scripts/main.py",
                    "to_input": "source_text",
                    "purpose": "Platform text feeds the script source_text argv value.",
                }
            ],
        },
    )

    assert result["passed"] is True
    prompt = captured["prompt"]
    assert "不要检查或裁决单个 bash command block" in prompt
    assert "argv key、placeholder、字段来源、字段类型、JSON quoting 或 shell quoting" in prompt
    assert "不得将整体语义审查判定为格式失败" in prompt
    assert "scripts/main.py" in prompt
    assert "user_text" in prompt


def test_skill_md_review_ignores_explicit_command_mapping_evidence():
    from backend.services.creator.contracts import _skill_md_blueprint_review_to_contract_results

    review = {
        "passed": False,
        "issues": [
            {
                "severity": "error",
                "blocking": True,
                "field": "workflow",
                "category": "command_mapping_explicit_evidence",
                "message": "command JSON argv value maps to the wrong stdout placeholder.",
                "evidence": (
                    "strict_json_argv_schema.required_keys contains source_text; "
                    "run_args_analysis.required_read_keys contains source_text; "
                    "incoming_edges.from_output is user_text, but the command uses placeholder old_stdout."
                ),
                "expected": "Bind source_text to incoming_edges.from_output user_text.",
                "minimal_edit": "Replace only the source_text value in the scripts/main.py command block.",
                "contract_impact": {"execution_closure": True},
            }
        ],
    }

    results = _skill_md_blueprint_review_to_contract_results(review)
    assert results == []


def _skill_md_block_stage_error(api, contracts, candidate: str, block: str):
    from backend.services.creator.command_normalizer import parse_skill_md_bash_command_blocks

    parsed_block = next(item for item in parse_skill_md_bash_command_blocks(candidate) if candidate[item.start:item.end] == block)
    full_block_text = candidate[parsed_block.start:parsed_block.end]
    command_text = parsed_block.content
    full_block_sha256 = api.hashlib.sha256(full_block_text.encode("utf-8")).hexdigest()
    locator_details = {
        "block_start": parsed_block.start,
        "block_end": parsed_block.end,
        "block_text": full_block_text,
        "block_sha256": full_block_sha256,
        "command_text": command_text,
        "current_block": command_text,
        "script_path": parsed_block.script_path or "scripts/two.py",
        "block_ordinal": 2,
        "block_locator": {
            "start": parsed_block.start,
            "end": parsed_block.end,
            "content_sha256": full_block_sha256,
            "content_excerpt": full_block_text[:1000],
        },
        "skill_md_block_repair_scope": {
            "block_start": parsed_block.start,
            "block_end": parsed_block.end,
            "block_text": full_block_text,
            "block_sha256": full_block_sha256,
            "command_text": command_text,
            "script_path": parsed_block.script_path or "scripts/two.py",
            "block_ordinal": 2,
        },
        "structured_checks": {"key_checks": [{"passed": False, "message": "missing required key"}]},
    }
    result = contracts.ContractCheckResult(
        id="skill_md.command_block.interface.missing_required_key.1",
        passed=False,
        target="SKILL.md:scripts/two.py:command_block",
        message="JSON argv missing required key; command_block json_argv key type issue",
        expected="required argv key exists",
        minimal_edit="repair only current command block",
        details=locator_details,
        layer="skill_md_command_block_interface",
    )
    return api.FileGenerationStageError(
        source="content_review",
        layer="skill_md_command_block_interface",
        detail="command_block json_argv key type issue",
        original=contracts.ContractValidationError("command block failed", [result]),
    )



def test_skill_md_block_repair_scope_uses_full_block_locator_from_real_parser():
    from backend.services.creator import contracts
    from backend.services.creator.command_normalizer import parse_skill_md_bash_command_blocks

    command = "python scripts/story_generator.py '{\"story_prompt\": \"{{user_request}}\", \"image_context\": \"\"}'"
    candidate = "# Demo\n\nIntro\n```bash\n" + command + "\n```\n\nDone\n"
    block = parse_skill_md_bash_command_blocks(candidate)[0]
    full_block = candidate[block.start:block.end]
    review = {
        "target_script_path": "scripts/story_generator.py",
        "passed": False,
        "command_block": block.content,
        "command_block_ordinal": 1,
        "issues": [{"message": "bad key", "field": "story_prompt"}],
    }

    results = contracts._skill_md_block_review_to_contract_results(
        review,
        block_text=full_block,
        command_text=block.content,
        block_locator=contracts._skill_md_block_locator(block, candidate),
    )
    locator = results[0].details["skill_md_block_repair_scope"]

    assert candidate[locator["block_start"]:locator["block_end"]] == locator["block_text"]
    assert hashlib.sha256(locator["block_text"].encode("utf-8")).hexdigest() == locator["block_sha256"]
    assert locator["command_text"] == command
    assert locator["block_text"] == full_block


def test_skill_md_exact_replacement_real_failure_replaces_only_current_block():
    from backend.services.creator import api
    from backend.services.creator.command_normalizer import parse_skill_md_bash_command_blocks

    first = "```bash\npython scripts/other.py '{\"topic\": \"{{user_request}}\"}'\n```\n"
    bad = "```bash\npython scripts/story_generator.py '{\"story_prompt\": \"{{user_request}}\", \"image_context\": \"\"}'\n```\n"
    fixed = "```bash\npython scripts/story_generator.py '{\"topic\": \"{{user_request}}\", \"image_context\": \"\"}'\n```\n"
    third = "```bash\npython scripts/final.py '{\"story\": \"{{story}}\"}'\n```\n"
    candidate = "# Demo\nBefore\n" + first + "Middle\n" + bad + "After\n" + third + "Done\n"
    block = next(item for item in parse_skill_md_bash_command_blocks(candidate) if item.script_path == "scripts/story_generator.py")
    locator = {
        "block_start": block.start,
        "block_end": block.end,
        "block_text": candidate[block.start:block.end],
        "block_sha256": hashlib.sha256(candidate[block.start:block.end].encode("utf-8")).hexdigest(),
    }

    repaired = api._replace_skill_md_command_block_exact(candidate, locator, fixed)

    assert repaired[block.start:block.start + len(fixed)] == fixed
    assert repaired[:block.start] == candidate[:block.start]
    assert repaired[block.start + len(fixed):] == candidate[block.end:]
    assert first in repaired
    assert third in repaired
    assert bad not in repaired


def test_skill_md_exact_replacement_does_not_rematch():
    from backend.services.creator import api

    source = inspect.getsource(api._replace_skill_md_command_block_exact)
    assert ".find(" not in source
    assert ".index(" not in source
    assert "re.search" not in source
    assert "fuzzy" not in source.lower()


def test_skill_md_exact_replacement_rejects_real_stale_locator():
    from backend.services.creator import api
    from backend.services.creator.command_normalizer import parse_skill_md_bash_command_blocks

    bad = "```bash\npython scripts/story_generator.py '{\"story_prompt\": \"{{user_request}}\", \"image_context\": \"\"}'\n```\n"
    fixed = "```bash\npython scripts/story_generator.py '{\"topic\": \"{{user_request}}\", \"image_context\": \"\"}'\n```\n"
    candidate = "# Demo\n" + bad + "Done\n"
    block = parse_skill_md_bash_command_blocks(candidate)[0]
    locator = {
        "block_start": block.start,
        "block_end": block.end,
        "block_text": candidate[block.start:block.end],
        "block_sha256": hashlib.sha256(candidate[block.start:block.end].encode("utf-8")).hexdigest(),
    }
    stale_candidate = candidate[:block.start] + bad.replace("story_prompt", "storyPrompt") + candidate[block.end:]

    with pytest.raises(ValueError, match="locator is stale"):
        api._replace_skill_md_command_block_exact(stale_candidate, locator, fixed)
    assert candidate[block.start:block.end] == locator["block_text"]


def test_skill_md_repair_scope_command_block_beats_full_format_words():
    from backend.services.creator import api, contracts

    first = "```bash\npython scripts/one.py '{\"a\":\"{{user_request}}\"}'\n```\n"
    bad = "```bash\npython scripts/two.py '{\"bad\":\"literal\"}'\n```\n"
    third = "```bash\npython scripts/three.py '{\"c\":\"{{two}}\"}'\n```\n"
    candidate = "---\nname: demo\ndescription: demo\n---\n# Demo\n" + first + "\n" + bad + "\n" + third
    stage_error = _skill_md_block_stage_error(api, contracts, candidate, bad)

    assert api._classify_skill_md_repair_scope(stage_error) == "command_block"


def test_skill_md_repair_scope_semantic_alignment_not_full_format():
    from backend.services.creator import api, contracts

    result = contracts.ContractCheckResult(
        id="skill_md.blueprint_alignment.missing_required_capability",
        passed=False,
        target="SKILL.md:workflow",
        message="遗漏蓝图要求的资源说明和功能项",
        expected="SKILL.md covers planned scripts and resources",
        minimal_edit="补充工作流职责说明，不修改 command block",
        details={"category": "blueprint_alignment"},
        layer="skill_md_blueprint_alignment",
    )
    stage_error = api.FileGenerationStageError(
        source="content_review",
        layer="skill_md_blueprint_alignment",
        detail="semantic alignment failed",
        original=contracts.ContractValidationError("semantic", [result]),
    )

    assert api._classify_skill_md_repair_scope(stage_error) == "semantic"


@pytest.mark.parametrize(
    "result_id,message,details,layer",
    [
        ("skill_md.frontmatter.unclosed", "frontmatter 未闭合", {}, "markdown_contract"),
        ("skill_md.markdown_body_structure.missing_body", "缺少正文", {}, "markdown_contract"),
        ("skill_md.markdown_body_structure.unclosed_fence", "全局 fence 未闭合", {}, "markdown_contract"),
        ("skill_md.any", "hard format", {"repair_strategy": "full_rewrite"}, "markdown_contract"),
        ("skill_md.any", "hard format layer", {}, "hard_format"),
    ],
)

def test_skill_md_repair_scope_full_format_for_global_structure(result_id, message, details, layer):
    from backend.services.creator import api, contracts

    result = contracts.ContractCheckResult(
        id=result_id,
        passed=False,
        target="SKILL.md",
        message=message,
        expected="valid markdown structure",
        minimal_edit="rewrite full markdown format",
        details=details,
        layer=layer,
    )
    stage_error = api.FileGenerationStageError(
        source="content_review",
        layer=layer,
        detail=message,
        original=contracts.ContractValidationError("format", [result]),
    )

    assert api._classify_skill_md_repair_scope(stage_error) == "full_format"


@pytest.mark.asyncio
async def test_generate_file_routes_skill_md_command_block_to_block_repair(monkeypatch, tmp_path):
    from backend.config import settings
    from backend.services.creator import api, contracts
    from backend.services.creator.common import GenerateFileRequest

    monkeypatch.setattr(settings, "skills_path", tmp_path)
    (tmp_path / "demo-skill").mkdir()

    first = "```bash\npython scripts/one.py '{\"a\":\"{{user_request}}\"}'\n```\n"
    bad = "```bash\npython scripts/two.py '{\"bad\":\"literal\"}'\n```\n"
    fixed = "```bash\npython scripts/two.py '{\"required\":\"{{a}}\"}'\n```\n"
    third = "```bash\npython scripts/three.py '{\"c\":\"{{required}}\"}'\n```\n"
    candidate = "---\nname: demo\ndescription: demo\n---\n# Demo\nBefore\n" + first + "Middle\n" + bad + "After\n" + third + "Done\n"
    stage_error = _skill_md_block_stage_error(api, contracts, candidate, bad)
    variants = []
    alignment_calls = 0

    async def fake_complete_creator_file_generation(**kwargs):
        variants.append(kwargs.get("prompt_variant"))
        if kwargs.get("prompt_variant") == "repair_skill_md_command_block":
            return fixed
        return candidate

    async def fake_alignment(**_kwargs):
        nonlocal alignment_calls
        alignment_calls += 1
        if alignment_calls == 1:
            raise stage_error
        return None

    async def forbidden_whole_file_repair(**_kwargs):
        raise AssertionError("whole-file repair must not be called for command_block scope")

    monkeypatch.setattr(api, "_complete_creator_file_generation", fake_complete_creator_file_generation)
    monkeypatch.setattr(api, "validate_file_contract", lambda **_kwargs: [])
    monkeypatch.setattr(api, "_validate_skill_md_against_existing_files", lambda *a, **k: None)
    monkeypatch.setattr(api, "_validate_skill_md_blueprint_alignment", fake_alignment)
    monkeypatch.setattr(api, "_repair_generated_file_with_feedback", forbidden_whole_file_repair)

    response = await api.generate_file(GenerateFileRequest(
        skill_name="demo-skill",
        file_path="SKILL.md",
        purpose="demo",
        blueprint_text="use scripts/one.py scripts/two.py scripts/three.py",
        conversation_history=[],
        role="skill_md",
        skill_plan_entry={},
    ))
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
    body = "".join(chunks)

    assert "repair_skill_md_command_block" in variants
    assert "rewrite_markdown_full_format" not in variants
    assert "required" in body and "{{a}}" in body
    assert "scripts/one.py" in body and "scripts/three.py" in body
    assert "Before" in body and "Middle" in body and "After" in body and "Done" in body


def test_skill_md_alignment_reviewer_does_not_repeat_format_validator():
    from backend.services.creator import contracts

    source = inspect.getsource(contracts._validate_skill_md_blueprint_alignment)
    before_model = source.split("_review_skill_md_blueprint_intent_with_model", 1)[0]

    assert "_check_skill_md_contract" not in before_model
    assert "_check_skill_md_fenced_command_contracts" not in before_model
    assert "_deterministic_skill_md_blueprint_alignment_checks" in before_model


def test_skill_md_command_block_locator_preserves_full_block_and_body_boundaries():
    from backend.services.creator.command_normalizer import parse_skill_md_bash_command_blocks
    from backend.services.creator.contracts import _skill_md_block_locator

    content = (
        "Before\n"
        "```bash\n"
        "python scripts/main.py '{\"input_key\": \"{{source_name}}\"}'\n"
        "```\n"
        "After\n"
    )

    block = parse_skill_md_bash_command_blocks(content)[0]
    locator = _skill_md_block_locator(block, content)

    assert locator["full_block_text"].startswith("```bash")
    assert locator["full_block_text"].endswith("```\n")
    assert locator["command_body_text"] == "python scripts/main.py '{\"input_key\": \"{{source_name}}\"}'\n"
    assert content[locator["block_start"]:locator["block_end"]] == locator["full_block_text"]
    assert content[locator["body_start"]:locator["body_end"]] == locator["command_body_text"]


def test_quoted_placeholder_json_remains_string_and_source_is_preserved():
    from backend.services.creator.contracts import _command_signature

    command = "python scripts/main.py '{\"input_key\": \"{{source_name}}\", \"other\": \"{{second}}\"}'"
    signature = _command_signature(command, "scripts/main.py")

    assert signature["arg_mode"] == "json_arg"
    assert signature["json_payload"] == {"input_key": "{{source_name}}", "other": "{{second}}"}
    assert signature["placeholders"] == {"input_key": "source_name", "other": "second"}
    assert signature["normalized_json_arg"] == '{"input_key": "{{source_name}}", "other": "{{second}}"}'
    assert signature["auto_fixes"] == []


def test_multiple_skill_md_block_failures_return_first_locator_without_whole_file_fallback():
    from backend.services.creator import api
    from backend.services.creator.contracts import ContractCheckResult, ContractValidationError

    first_block = "```bash\npython scripts/one.py '{}'\n```"
    second_block = "```bash\npython scripts/two.py '{}'\n```"
    first_hash = hashlib.sha256(first_block.encode("utf-8")).hexdigest()
    second_hash = hashlib.sha256(second_block.encode("utf-8")).hexdigest()
    first = ContractCheckResult(
        id="first",
        passed=False,
        target="SKILL.md:scripts/one.py:command_block",
        message="first failed",
        expected="fix first",
        minimal_edit="repair first",
        layer="skill_md_command_block_interface",
        details={
            "script_path": "scripts/one.py",
            "block_text": first_block,
            "command_text": "python scripts/one.py '{}'",
            "block_start": 20,
            "block_end": 20 + len(first_block),
            "block_sha256": first_hash,
        },
    )
    second = ContractCheckResult(
        id="second",
        passed=False,
        target="SKILL.md:scripts/two.py:command_block",
        message="second failed",
        expected="fix second",
        minimal_edit="repair second",
        layer="skill_md_command_block_interface",
        details={
            "script_path": "scripts/two.py",
            "block_text": second_block,
            "command_text": "python scripts/two.py '{}'",
            "block_start": 100,
            "block_end": 100 + len(second_block),
            "block_sha256": second_hash,
        },
    )

    locator = api._single_skill_md_command_block_failure(ContractValidationError("failed", [second, first]))

    assert locator is not None
    assert locator["script_path"] == "scripts/one.py"
    assert locator["block_start"] == 20
    assert locator["failure_reasons"] == [
        {
            "id": "first",
            "message": "first failed",
            "expected": "fix first",
            "minimal_edit": "repair first",
            "details": first.details,
        }
    ]


def test_skill_md_each_block_review_uses_only_current_script_context():
    from backend.services.creator import contracts

    source = inspect.getsource(contracts._validate_skill_md_blueprint_alignment)
    block_loop = source.split("for ordinal, block in enumerate(command_blocks", 1)[1]
    before_stdout_update = block_loop.split("prior_stdout.append", 1)[0]

    assert "script_paths=[script_path]" in before_stdout_update
    assert "script_paths=required_script_paths" not in before_stdout_update
    assert "script_paths=[path" not in before_stdout_update


@pytest.mark.asyncio
async def test_generate_file_repairs_first_of_multiple_blocks_after_real_format_validation(monkeypatch, tmp_path):
    from backend.config import settings
    from backend.services.creator import api, contracts
    from backend.services.creator.command_normalizer import parse_skill_md_bash_command_blocks
    from backend.services.creator.common import GenerateFileRequest

    monkeypatch.setattr(settings, "skills_path", tmp_path)
    (tmp_path / "demo-skill").mkdir()

    bad_one = "```bash\npython scripts/one.py '{\"bad\":\"literal\"}'\n```\n"
    bad_two = "```bash\npython scripts/two.py '{\"bad\":\"literal\"}'\n```\n"
    fixed_one = "```bash\npython scripts/one.py '{\"required\":\"{{user_request}}\"}'\n```\n"
    candidate = "---\nname: demo\ndescription: demo\n---\n# Demo\n" + bad_one + "Between\n" + bad_two + "Done\n"
    blocks = parse_skill_md_bash_command_blocks(candidate)
    results = []
    for block in blocks:
        full_block = candidate[block.start:block.end]
        script_path = str(block.script_path)
        results.append(contracts.ContractCheckResult(
            id=f"skill_md.command_block.interface.issue.{script_path}",
            passed=False,
            target=f"SKILL.md:{script_path}:command_block",
            message=f"{script_path} failed",
            expected="repair only this block",
            minimal_edit="repair only this block",
            details={
                "script_path": script_path,
                "block_text": full_block,
                "full_block_text": full_block,
                "command_text": block.content,
                "command_body_text": block.content,
                "block_start": block.start,
                "block_end": block.end,
                "body_start": block.body_start,
                "body_end": block.body_end,
                "block_sha256": hashlib.sha256(full_block.encode("utf-8")).hexdigest(),
            },
            layer="skill_md_command_block_interface",
        ))
    stage_error = api.FileGenerationStageError(
        source="content_review",
        layer="skill_md_command_block_interface",
        detail="two command blocks failed",
        original=contracts.ContractValidationError("multi block", list(reversed(results))),
    )
    variants = []
    alignment_calls = 0

    async def fake_complete_creator_file_generation(**kwargs):
        variants.append(kwargs.get("prompt_variant"))
        if kwargs.get("prompt_variant") == "repair_skill_md_command_block":
            return fixed_one
        return candidate

    async def fake_alignment(**_kwargs):
        nonlocal alignment_calls
        alignment_calls += 1
        if alignment_calls == 1:
            raise stage_error
        return None

    monkeypatch.setattr(api, "_complete_creator_file_generation", fake_complete_creator_file_generation)
    monkeypatch.setattr(api, "_validate_skill_md_against_existing_files", lambda *a, **k: None)
    monkeypatch.setattr(api, "_validate_skill_md_blueprint_alignment", fake_alignment)

    response = await api.generate_file(GenerateFileRequest(
        skill_name="demo-skill",
        file_path="SKILL.md",
        purpose="demo",
        blueprint_text="use scripts/one.py scripts/two.py",
        conversation_history=[],
        role="skill_md",
        skill_plan_entry={},
    ))
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
    body = "".join(chunks)

    assert "repair_skill_md_command_block" in variants
    assert "scripts/one.py" in body and "required" in body
    assert "scripts/two.py" in body and "literal" in body
    assert alignment_calls == 2


@pytest.mark.asyncio
async def test_command_block_repair_receives_full_fenced_block(monkeypatch, tmp_path):
    from backend.config import settings
    from backend.services.creator import api, contracts
    from backend.services.creator.command_normalizer import parse_skill_md_bash_command_blocks
    from backend.services.creator.common import GenerateFileRequest

    monkeypatch.setattr(settings, "skills_path", tmp_path)
    (tmp_path / "demo-skill").mkdir()
    bad = "```bash\npython scripts/main.py '{\"bad\":\"literal\"}'\n```\n"
    fixed = "```bash\npython scripts/main.py '{\"input\":\"{{user_request}}\"}'\n```\n"
    candidate = "---\nname: demo\ndescription: demo\n---\n# Demo\n" + bad + "Done\n"
    block = parse_skill_md_bash_command_blocks(candidate)[0]
    full_block = candidate[block.start:block.end]
    result = contracts.ContractCheckResult(
        id="skill_md.command_block.interface.issue",
        passed=False,
        target="SKILL.md:scripts/main.py:command_block",
        message="failed",
        expected="repair only this block",
        minimal_edit="repair only this block",
        details={
            "script_path": "scripts/main.py",
            "block_text": full_block,
            "full_block_text": full_block,
            "command_text": block.content,
            "command_body_text": block.content,
            "block_start": block.start,
            "block_end": block.end,
            "body_start": block.body_start,
            "body_end": block.body_end,
            "block_sha256": hashlib.sha256(full_block.encode("utf-8")).hexdigest(),
        },
        layer="skill_md_command_block_interface",
    )
    stage_error = api.FileGenerationStageError(
        source="content_review",
        layer="skill_md_command_block_interface",
        detail="block failed",
        original=contracts.ContractValidationError("block", [result]),
    )
    seen = {}

    async def fake_complete_creator_file_generation(**kwargs):
        return candidate

    alignment_calls = 0

    async def fake_alignment(**_kwargs):
        nonlocal alignment_calls
        alignment_calls += 1
        if alignment_calls == 1:
            raise stage_error
        return None

    async def fake_repair_skill_md_command_block(**kwargs):
        seen["block_text"] = kwargs["block_text"]
        return fixed

    monkeypatch.setattr(api, "_complete_creator_file_generation", fake_complete_creator_file_generation)
    monkeypatch.setattr(api, "_validate_skill_md_against_existing_files", lambda *a, **k: None)
    monkeypatch.setattr(api, "_validate_skill_md_blueprint_alignment", fake_alignment)
    monkeypatch.setattr(api, "_repair_skill_md_command_block", fake_repair_skill_md_command_block)

    response = await api.generate_file(GenerateFileRequest(
        skill_name="demo-skill",
        file_path="SKILL.md",
        purpose="demo",
        blueprint_text="use scripts/main.py",
        conversation_history=[],
        role="skill_md",
        skill_plan_entry={},
    ))
    async for _chunk in response.body_iterator:
        pass

    assert seen["block_text"].startswith("```bash")
    assert seen["block_text"].rstrip().endswith("```")
    assert "python scripts/main.py" in seen["block_text"]
    assert not seen["block_text"].startswith("python scripts/main.py")


def test_repaired_block_effective_command_lines_ignore_comments_but_reject_two_commands():
    from backend.services.creator import api

    api._validate_repaired_skill_md_command_block(
        "```bash\n# execution entry\npython scripts/main.py '{\"input\":\"{{input}}\"}'\n```",
        script_path="scripts/main.py",
    )

    with pytest.raises(ValueError, match="exactly one command line"):
        api._validate_repaired_skill_md_command_block(
            "```bash\npython scripts/a.py '{}'\npython scripts/b.py '{}'\n```",
            script_path="scripts/a.py",
        )
