"""Deterministic platform IO contract shared with Creator prompts.

The sandbox runtime is the source of truth.  Creator code imports this module
only to describe that runtime contract to generation and repair models.
"""

from __future__ import annotations

from typing import Any


_LEGACY_OUTPUT_VALUE_SCHEMAS: dict[str, dict[str, Any]] = {
    "text": {"type": "string", "minLength": 1},
    "markdown": {"type": "string", "minLength": 1},
    "image_path": {"type": "string", "minLength": 1},
    "pdf_path": {"type": "string", "minLength": 1},
    "docx_path": {"type": "string", "minLength": 1},
    "pptx_path": {"type": "string", "minLength": 1},
    "html_path": {"type": "string", "minLength": 1},
    "image_paths": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
    "file_paths": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
    "file_outputs": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
}


def normalize_platform_output_sinks(contract: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the sole canonical representation of platform output sinks.

    String declarations retain their historical single-assignment behavior.
    Explicit declarations must use a compatible cardinality/write-semantics pair.
    """
    value = contract or {}
    boundary = value.get("platform_skill_boundary", value)
    declarations = boundary.get("final_output_fields") or [] if isinstance(boundary, dict) else []
    sinks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for declaration in declarations:
        if isinstance(declaration, str):
            name = declaration.strip()
            schema = dict(_LEGACY_OUTPUT_VALUE_SCHEMAS.get(name, {"type": "string", "minLength": 1}))
            cardinality, write_semantics = "one", "single"
        elif isinstance(declaration, dict):
            name = str(declaration.get("name") or declaration.get("field") or declaration.get("port_id") or declaration.get("id") or "").strip()
            raw_schema = declaration.get("value_schema")
            if not isinstance(raw_schema, dict):
                raw_schema = declaration.get("contract")
            schema = dict(raw_schema) if isinstance(raw_schema, dict) and raw_schema else dict(_LEGACY_OUTPUT_VALUE_SCHEMAS.get(name, {"type": "string", "minLength": 1}))
            cardinality = str(declaration.get("cardinality") or "one").strip().lower()
            write_semantics = str(declaration.get("write_semantics") or ("single" if cardinality == "one" else "")).strip().lower()
        else:
            continue
        if not name or name in seen:
            continue
        if (cardinality, write_semantics) not in {("one", "single"), ("many", "append"), ("many", "collect")}:
            raise ValueError(f"invalid platform output sink semantics: {name}")
        seen.add(name)
        sinks.append({"name": name, "value_schema": schema, "cardinality": cardinality, "write_semantics": write_semantics})
    return sinks


def platform_output_names(contract: dict[str, Any] | None) -> list[str]:
    return [sink["name"] for sink in normalize_platform_output_sinks(contract)]


def get_platform_output_sink(contract: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    return next((sink for sink in normalize_platform_output_sinks(contract) if sink["name"] == name), None)


def value_matches_platform_schema(value: Any, schema: dict[str, Any]) -> bool:
    """Validate the intentionally small JSON-schema subset used by output sinks."""
    schema_type = schema.get("type")
    if schema_type == "string":
        return isinstance(value, str) and (not schema.get("minLength") or len(value.strip()) >= int(schema["minLength"]))
    if schema_type == "array":
        if not isinstance(value, list) or len(value) < int(schema.get("minItems") or 0):
            return False
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        return all(value_matches_platform_schema(item, item_schema) for item in value)
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    return True


def commit_platform_output_emissions(contract: dict[str, Any], emissions: list[dict[str, Any]]) -> dict[str, Any]:
    """Transactionally compose emissions in their canonical interface/edge order."""
    grouped: dict[str, list[Any]] = {}
    for emission in emissions:
        grouped.setdefault(str(emission.get("sink") or ""), []).append(emission.get("value"))
    committed: dict[str, Any] = {}
    for sink in normalize_platform_output_sinks(contract):
        values = grouped.get(sink["name"], [])
        if not values:
            continue
        if len(values) > 1 and sink["cardinality"] == "one":
            raise ValueError(f"multiple emissions for single sink: {sink['name']}")
        if not all(value_matches_platform_schema(value, sink["value_schema"]) for value in values):
            raise ValueError(f"invalid emission value for sink: {sink['name']}")
        if sink["write_semantics"] == "append":
            committed[sink["name"]] = "".join(values)
        elif sink["write_semantics"] == "collect":
            committed[sink["name"]] = list(values)
        else:
            committed[sink["name"]] = values[0]
    return committed


def build_platform_io_contract() -> dict[str, Any]:
    """Return the immutable sandbox artifact IO contract for Creator prompts."""
    return {
        "environment": {
            "OUTPUT_DIR": "already points to the final outputs directory for the current skill workspace",
        },
        "platform_skill_boundary": {
            "input_envelope_fields": ["user_request", "input", "text", "payload", "fields", "options", "input_files", "files", "resources"],
            "preferred_structured_input_root": "fields",
            "final_output_fields": ["text", "markdown", "image_path", "image_paths", "pdf_path", "docx_path", "pptx_path", "html_path", "file_paths", "file_outputs"],
            "protocol_notes": [
                "Platform input_envelope_fields are source slots the platform can provide to a generated SKILL.",
                "Platform final_output_fields are terminal slots the platform can consume from the final stdout JSON.",
                "Platform boundary fields are not a whitelist for script argv keys.",
                "SKILL.md should document which argv.<key> comes from which platform input slot, reference file, asset file, literal default, runtime constant, or previous stdout field.",
                "Scripts are encouraged to use the argv keys already mapped in SKILL.md, but may define task-specific optional/default/config parameters.",
                "Internal script-to-script fields are recommended shared vocabulary only, not a hard validation contract.",
                "Command argv must express dynamic dataflow with placeholders instead of literal runtime data.",
            ],
        },
        "hard_rules": [
            "OUTPUT_DIR already points to final outputs directory.",
            "Do not append 'outputs' to OUTPUT_DIR.",
            "Do not use os.path.join(OUTPUT_DIR, 'outputs').",
            "Do not use os.path.join(output_dir, 'outputs').",
            "Do not replace '/tmp/' with 'outputs/'.",
            "Runtime artifact helpers write artifacts under OUTPUT_DIR.",
            "Helper filename parameters must be basenames only, for example filename='report.pdf'.",
            "Do not pass full paths or absolute paths to filename=.",
            "Prefer returning helper results unchanged, or forwarding result['pdf_path'] and result['file_outputs'] unchanged.",
            "Do not manually rewrite helper-returned pdf_path/file_outputs.",
            "Artifact allowed roots are outputs/ and assets/generated/.",
            "Absolute paths under the current skill workspace outputs/ or assets/generated/ directories are valid artifact paths when the files exist.",
        ],
        "allowed_artifact_roots": ["outputs/", "assets/generated/"],
        "helper_filename_rule": "basename only, e.g. 'report.pdf', never a full path",
        "valid_absolute_paths": [
            "<current_skill_workspace>/outputs/<file>",
            "<current_skill_workspace>/assets/generated/<file>",
        ],
        "forbidden_patterns": [
            "os.path.join(OUTPUT_DIR, 'outputs')",
            "os.path.join(output_dir, 'outputs')",
            ".replace('/tmp/', 'outputs/')",
            "filename=full_path",
            "filename=absolute_path",
        ],
    }


def platform_io_contract_prompt_text() -> str:
    """Render the platform IO contract as concise prompt text."""
    contract = build_platform_io_contract()
    lines = [
        "Platform IO Contract (deterministic, immutable; sandbox is source of truth):",
        "- OUTPUT_DIR already points to final outputs directory.",
        "- Do not append 'outputs' to OUTPUT_DIR; never use os.path.join(OUTPUT_DIR, 'outputs') or os.path.join(output_dir, 'outputs').",
        "- Do not replace '/tmp/' with 'outputs/'.",
        "- Runtime artifact helpers write artifacts under OUTPUT_DIR.",
        "- Helper filename= arguments must be basenames only; never filename=full_path or filename=absolute_path.",
        "- Prefer return helper result unchanged, or forward result['pdf_path'] and result['file_outputs'] unchanged.",
        "- Do not manually rewrite helper-returned pdf_path/file_outputs.",
        "- Artifact allowed roots: outputs/ and assets/generated/.",
        "- Absolute paths under current skill workspace outputs/ or assets/generated/ are valid if the files exist.",
        "- Platform boundary input source slots: user_request, input, text, payload, fields, options, input_files, files, resources.",
        "- Platform final output terminal slots: text, markdown, image_path, image_paths, pdf_path, docx_path, pptx_path, html_path, file_paths, file_outputs.",
        "- Platform boundary fields are source/terminal slots, not a whitelist for script argv keys.",
        "- SKILL.md should document how platform source slots map to script argv keys.",
        "- Scripts are encouraged to use the mapped argv keys, but may define task-specific optional/default/config parameters.",
        "- Internal fields are recommended shared vocabulary, not hard validation schema.",
        "- Command argv must express dynamic dataflow with placeholders instead of literal runtime data.",
        "- Contract payload: " + str(contract),
    ]
    return "\n".join(lines)
