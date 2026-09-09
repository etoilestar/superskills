"""Deterministic platform IO contract shared with Creator prompts.

The sandbox runtime is the source of truth.  Creator code imports this module
only to describe that runtime contract to generation and repair models.
"""

from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any


_LEGACY_OUTPUT_VALUE_SCHEMAS: dict[str, dict[str, Any]] = {
    "text": {"type": "string"},
    "markdown": {"type": "string"},
    "image_path": {"type": "string", "minLength": 1},
    "pdf_path": {"type": "string", "minLength": 1},
    "docx_path": {"type": "string", "minLength": 1},
    "pptx_path": {"type": "string", "minLength": 1},
    "html_path": {"type": "string", "minLength": 1},
    "image_paths": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
    "file_paths": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
    "file_outputs": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
}

# Transform is a runtime capability, not interface planning information.
# Runtime binding queries this registry only when an interface is executed.
OUTPUT_TRANSFORM_REGISTRY: dict[str, dict[str, Any]] = {
    "json_serialize": {
        "input_types": ("object", "json", "array"),
        "result_type": "text",
    },
    "file_write": {
        "input_types": ("object", "json"),
        "result_type": "file",
    },
    "markdown_render": {
        "input_types": ("structured_data", "object", "json"),
        "result_type": "markdown",
    },
    # Compatibility adapter retained for existing generated plans.
    "file_collect": {
        "input_types": ("artifact", "file", "file_path", "list[file_path]"),
        "result_type": "file",
    },
}


def resolve_runtime_output_transform(
    *, source_type: str, target_type: str, allowed_transforms: list[str] | None = None,
) -> str | None:
    """Resolve representation adaptation at execution time.

    ``None`` means the representations bind directly. A missing runtime
    capability is an execution failure, never an Interface Plan failure.
    """
    aliases = {"string": "text", "artifact": "file", "file_path": "file", "list[file_path]": "file"}
    source = aliases.get(str(source_type), str(source_type))
    target = aliases.get(str(target_type), str(target_type))
    if source == target:
        return None
    permitted = set(allowed_transforms) if allowed_transforms is not None else set(OUTPUT_TRANSFORM_REGISTRY)
    for name, capability in OUTPUT_TRANSFORM_REGISTRY.items():
        accepted = capability.get("input_types", ())
        if name in permitted and str(source_type) in accepted and capability.get("result_type") == target:
            return name
    raise RuntimeError(f"no runtime output adaptation from {source_type} to {target_type}")


_PORTABLE_OUTPUT_MAPPING_RE = re.compile(
    r"<!--\s*runtime-output-mapping:\s*(\{.*?\})\s*-->", re.DOTALL,
)


def platform_output_delivery_channel(contract: dict[str, Any], target: str) -> str:
    """Collapse concrete platform sinks into the two user-visible channels."""
    sink = get_platform_output_sink(contract, target)
    if not sink:
        raise ValueError(f"unknown platform output sink: {target}")
    return "download" if sink["semantic_type"] == "file" else "display"


def render_runtime_output_mapping(
    script_path: str, mapping: dict[str, list[str]], contract: dict[str, Any] | None = None,
) -> str:
    """Compile Creator interface facts into a portable two-channel delivery record."""
    platform_contract = contract or build_platform_io_contract()
    bindings = [
        {
            "source": source,
            "target": target,
            "delivery": platform_output_delivery_channel(platform_contract, target),
        }
        for target, sources in sorted(mapping.items())
        for source in sources
    ]
    payload = {"version": 2, "script": script_path, "bindings": bindings}
    return "<!-- runtime-output-mapping: " + json.dumps(payload, ensure_ascii=False, sort_keys=True) + " -->"


def parse_runtime_output_mappings(skill_text: str) -> list[dict[str, str]]:
    """Read portable output bindings using only the installed Skill artifact."""
    bindings: list[dict[str, str]] = []
    for match in _PORTABLE_OUTPUT_MAPPING_RE.finditer(skill_text or ""):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("version") not in {1, 2}:
            continue
        script = str(payload.get("script") or "").strip()
        for item in payload.get("bindings") or []:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "").strip()
            target = str(item.get("target") or "").strip()
            delivery = str(item.get("delivery") or "").strip()
            if not delivery and target:
                # Read existing v1 Skill artifacts without Creator state.
                delivery = "download" if target.endswith("_path") or target in {"file_outputs", "file_paths", "image_paths"} else "display"
            if script and source and target and delivery in {"display", "download"}:
                bindings.append({"script": script, "source": source, "target": target, "delivery": delivery})
    return bindings


def _runtime_output_value_type(value: Any) -> str:
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "unknown"


def _adapt_runtime_output_value(value: Any, sink: dict[str, Any]) -> Any:
    if value_matches_platform_schema(value, sink["value_schema"]):
        return value
    transform = resolve_runtime_output_transform(
        source_type=_runtime_output_value_type(value),
        target_type=sink["semantic_type"],
        allowed_transforms=sink["allowed_transforms"],
    )
    if transform is None:
        # ``None`` is the resolver's explicit direct-binding result.  Value
        # constraints are checked by the commit phase rather than being
        # misreported as a missing transform capability.
        return value
    if transform == "json_serialize":
        return json.dumps(value, ensure_ascii=False)
    raise RuntimeError(f"runtime output transform is not executable: {transform}")


def _default_output_semantics(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Infer compatibility metadata for legacy declarations only."""
    if name in {"file_outputs", "file_paths", "image_paths"} or name.endswith("_path"):
        return {
            "semantic_type": "file",
            "accepted_source_types": ["artifact", "file", "file_path", "list[file_path]", "object", "json"],
            "allowed_transforms": ["file_collect", "file_write"],
        }
    semantic_type = "markdown" if name == "markdown" else "text" if schema.get("type") == "string" else str(schema.get("type") or "unknown")
    allowed = ["markdown_render"] if semantic_type == "markdown" else ["json_serialize", "markdown_render"] if semantic_type == "text" else []
    return {
        "semantic_type": semantic_type,
        "accepted_source_types": [semantic_type, "string", "json", "object", "structured_data"],
        "allowed_transforms": allowed,
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
        defaults = _default_output_semantics(name, schema)
        if isinstance(declaration, dict):
            semantic_type = str(declaration.get("semantic_type") or defaults["semantic_type"]).strip()
            accepted = declaration.get("accepted_source_types", defaults["accepted_source_types"])
            allowed = declaration.get("allowed_transforms", defaults["allowed_transforms"])
        else:
            semantic_type, accepted, allowed = defaults["semantic_type"], defaults["accepted_source_types"], defaults["allowed_transforms"]
        sinks.append({
            "name": name, "semantic_type": semantic_type,
            "accepted_source_types": list(accepted), "allowed_transforms": list(allowed),
            "value_schema": schema, "cardinality": cardinality,
            "write_semantics": write_semantics,
        })
    return sinks


def platform_output_names(contract: dict[str, Any] | None) -> list[str]:
    return [sink["name"] for sink in normalize_platform_output_sinks(contract)]


def project_function_item_outputs_to_platform_contract(
    contract: dict[str, Any], function_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Declare typed, externally deliverable FunctionItem outputs as sinks.

    The projection is deliberately structural: it uses the declared port name,
    type, and role, never a list of business-specific output names.  An
    ``intermediate_output`` remains private to members.  Legacy untyped string
    ports are not projected because doing so would let arbitrary stdout keys
    expand the platform boundary.
    """
    projected = deepcopy(contract)
    boundary = projected.get("platform_skill_boundary", projected)
    if not isinstance(boundary, dict):
        return projected
    declarations = boundary.setdefault("final_output_fields", [])
    if not isinstance(declarations, list):
        return projected
    declared = set(platform_output_names(projected))
    aliases = {
        "string": "text", "str": "text", "file_path": "file",
        "artifact": "file", "image": "file",
    }
    supported = {"text", "markdown", "file"}
    for item in function_items:
        for output in item.get("outputs", []) if isinstance(item, dict) else []:
            if not isinstance(output, dict):
                continue
            name = str(output.get("name") or output.get("port_id") or output.get("field") or "").strip()
            role = str(output.get("role") or "runtime_output").strip()
            contract_schema = output.get("contract") if isinstance(output.get("contract"), dict) else {}
            raw_type = output.get("type") or contract_schema.get("type")
            type_name = aliases.get(str(raw_type or "").strip().lower(), str(raw_type or "").strip().lower())
            is_many = type_name.startswith(("list[", "array"))
            element_type = type_name[5:-1] if type_name.startswith("list[") else type_name
            element_type = aliases.get(element_type, element_type)
            if (not name or name in declared or role == "intermediate_output"
                    or element_type not in supported):
                continue
            value_schema = (
                {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1}
                if is_many else {"type": "string", "minLength": 1}
            )
            semantic_type = "file" if element_type == "file" else element_type
            declarations.append({
                "name": name,
                "semantic_type": semantic_type,
                "accepted_source_types": [str(raw_type), element_type],
                "allowed_transforms": [],
                "value_schema": value_schema,
                "cardinality": "one",
                "write_semantics": "single",
            })
            declared.add(name)
    return projected


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
    """Validate and compose already structurally ordered terminal emissions."""
    grouped: dict[str, list[Any]] = {}
    sinks = {sink["name"]: sink for sink in normalize_platform_output_sinks(contract)}
    for emission in emissions:
        sink_name = str(emission.get("sink") or "")
        sink = sinks.get(sink_name)
        if sink is None:
            raise ValueError(f"unknown platform output sink: {sink_name}")
        grouped.setdefault(sink_name, []).append(_adapt_runtime_output_value(emission.get("value"), sink))
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


def project_and_commit_skill_outputs(
    contract: dict[str, Any], skill_text: str, completed_outputs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Commit outputs from portable mappings embedded in the Skill artifact."""
    emissions: list[dict[str, Any]] = []
    for order, binding in enumerate(parse_runtime_output_mappings(skill_text)):
        outputs = completed_outputs.get(binding["script"])
        if not isinstance(outputs, dict) or binding["source"] not in outputs:
            raise ValueError(f"missing portable terminal emission value: binding {order}")
        value = outputs[binding["source"]]
        if binding["delivery"] == "display":
            if isinstance(value, str):
                display_value = value
            elif value is None or isinstance(value, (dict, list, int, float, bool)):
                display_value = json.dumps(value, ensure_ascii=False)
            else:
                raise RuntimeError(f"output cannot be delivered to display: {binding['source']}")
            emissions.append({"sink": "text", "value": display_value, "order_key": order})
        else:
            emissions.append({"sink": binding["target"], "value": value, "order_key": order})
    return commit_platform_output_emissions(contract, emissions) if emissions else {}


def project_and_commit_platform_outputs(
    contract: dict[str, Any],
    terminal_edges: list[dict[str, Any]],
    completed_outputs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Project successful member outputs in authoritative terminal-edge order.

    ``completed_outputs`` may be populated in any execution-completion order.  The
    immutable ResponsibilityGraph edge list is the transport ordering authority.
    """
    emissions: list[dict[str, Any]] = []
    for edge_index, edge in enumerate(terminal_edges):
        if str(edge.get("to_node") or "") != "platform_output_node":
            continue
        member = str(edge.get("from_node") or "")
        output = str(edge.get("from_output") or "")
        sink = str(edge.get("to_input") or "")
        member_outputs = completed_outputs.get(member)
        if not isinstance(member_outputs, dict) or output not in member_outputs:
            raise ValueError(f"missing terminal emission value: edge {edge_index}")
        emissions.append({
            "sink": sink,
            "value": member_outputs[output],
            "order_key": edge_index,
        })
    return commit_platform_output_emissions(contract, emissions)


def build_platform_io_contract() -> dict[str, Any]:
    """Return the immutable sandbox artifact IO contract for Creator prompts."""
    return {
        "environment": {
            "OUTPUT_DIR": "already points to the final outputs directory for the current skill workspace",
        },
        "platform_skill_boundary": {
            "input_envelope_fields": ["user_request", "input", "text", "payload", "options", "input_files", "files", "resources"],
            "input_source_semantics": {
                "freeform_request": {
                    "canonical": "user_request",
                    "representations": ["user_request", "input", "text"],
                    "globally_required": False,
                },
                "runtime_files": {
                    "canonical": "input_files",
                    "representations": ["input_files", "files"],
                    "globally_required": False,
                },
                "runtime_options": {"canonical": "options", "globally_required": False},
                "runtime_resources": {"canonical": "resources", "globally_required": False},
            },
            "final_output_fields": [
                {"name": "text", "semantic_type": "text", "accepted_source_types": ["string", "text", "json", "object"], "allowed_transforms": ["json_serialize", "markdown_render"], "value_schema": {"type": "string"}, "cardinality": "many", "write_semantics": "append"},
                {"name": "markdown", "value_schema": {"type": "string"}, "cardinality": "many", "write_semantics": "append"},
                {"name": "image_path", "value_schema": {"type": "string", "minLength": 1}, "cardinality": "one", "write_semantics": "single"},
                {"name": "image_paths", "value_schema": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1}, "cardinality": "one", "write_semantics": "single"},
                {"name": "pdf_path", "value_schema": {"type": "string", "minLength": 1}, "cardinality": "one", "write_semantics": "single"},
                {"name": "docx_path", "value_schema": {"type": "string", "minLength": 1}, "cardinality": "one", "write_semantics": "single"},
                {"name": "pptx_path", "value_schema": {"type": "string", "minLength": 1}, "cardinality": "one", "write_semantics": "single"},
                {"name": "html_path", "value_schema": {"type": "string", "minLength": 1}, "cardinality": "one", "write_semantics": "single"},
                {"name": "file_paths", "value_schema": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1}, "cardinality": "one", "write_semantics": "single"},
                {"name": "file_outputs", "value_schema": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1}, "cardinality": "one", "write_semantics": "single"},
            ],
            "protocol_notes": [
                "Platform input_envelope_fields are source slots the platform can provide to a generated SKILL.",
                "input_source_semantics is descriptive canonical/relationship guidance, not a closed whitelist; input_envelope_fields remains the legal source domain.",
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
        "- Platform boundary input source slots: user_request, input, text, payload, options, input_files, files, resources.",
        "PLATFORM INPUT SEMANTICS",
        "- No platform input source is globally required.",
        "- user_request is the canonical representation of a runtime free-form natural-language request when such input is required by the Skill.",
        "- input/text may be compatibility representations of that same request and must not be treated as independent business values merely because their names match FunctionItem ports.",
        "- input_files represents runtime-uploaded file or multimodal content; files may be its derived compatibility view.",
        "- Business inputs declared by a Skill are independent top-level semantic slots; there is no default structured-parameter wrapper.",
        "- A Skill may consume zero, one, or multiple source families according to confirmed requirements.",
        "- INPUT SOURCE SEMANTICS ARE DESCRIPTIVE, NOT A CLOSED WHITELIST.",
        "- input_source_semantics describes known canonical/related representations; it does not remove or forbid any legal source declared in input_envelope_fields.",
        "- When confirmed requirements establish that another legal platform source owns the required semantic value, the Interface Planner may select it; do not invent an unknown platform source.",
        "- Platform final output terminal slots: text, markdown, image_path, image_paths, pdf_path, docx_path, pptx_path, html_path, file_paths, file_outputs.",
        "- Platform boundary fields are source/terminal slots, not a whitelist for script argv keys.",
        "- SKILL.md should document how platform source slots map to script argv keys.",
        "- Scripts are encouraged to use the mapped argv keys, but may define task-specific optional/default/config parameters.",
        "- Internal fields are recommended shared vocabulary, not hard validation schema.",
        "- Command argv must express dynamic dataflow with placeholders instead of literal runtime data.",
        "- Contract payload: " + str(contract),
    ]
    return "\n".join(lines)
