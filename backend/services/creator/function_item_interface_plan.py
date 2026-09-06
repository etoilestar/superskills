"""FunctionItem Interface Intent Plan protocol for Creator graph planning.

The Blueprint already decomposes the complete system into frozen executable
FunctionItems. This module plans and validates only semantic interaction intents
between those already-frozen FunctionItems and the platform; it does not create
another subsystem decomposition or infer business semantics.
"""

from __future__ import annotations

import json
import logging
import ast
from collections.abc import Awaitable, Callable
from typing import Any

from ..skill_plan import GraphValidationError, normalize_structured_function_items
from ..platform_io_contract import (
    get_platform_output_sink, platform_output_names,
)
from .bounded_refinement import (
    BoundedRefinementFailed,
    CandidateEvaluation,
    bounded_refine_candidate,
)

logger = logging.getLogger(__name__)
ModelCall = Callable[[list[dict[str, str]], str], Awaitable[str]]
AUTHORITY_CONTRACT = """CROSS-STAGE AUTHORITY CONTRACT

Confirmed user requirements define task intent.
Blueprint and FunctionItem planning define frozen executable responsibilities and logical ports.
Requirement Projection assigns requirement channels and executable ownership.
Interface Planner owns only platform-boundary logical-port binding decisions.
Frozen Graph dependencies own member-to-member bindings, which are projected deterministically.
Interface Planner Correction may repair only the Interface layer when a previous Planner result fails deterministic Interface acceptance.
Interface Reviewer evaluates semantic correctness only after deterministic Interface validity has been established.
Critic diagnoses semantic or Graph blocking facts and states only the required postcondition.
Repair Generator repairs only the editable Interface layer.
Graph materialization deterministically resolves already-declared logical bindings to opaque runtime endpoint IDs and validates execution topology.
Graph materialization does not reselect semantic business ports.
Tool Planner owns concrete tool/helper binding.
No later stage may silently revise an upstream frozen fact outside its declared authority."""
INTERFACE_CONTRACT_SCOPE_CONTRACT = """The FunctionItem contract is already frozen.

The planner is responsible for creating semantic bindings between existing
contract ports. It determines only:

source port -> target port

Interface Planner MUST NOT:
- create new FunctionItems
- modify FunctionItem inputs
- modify FunctionItem outputs
- reinterpret business responsibility
- invent new output fields
- invent transformation names
- design runtime conversion logic

Interface Planner ONLY creates platform-boundary connections:

source port
    ->
target port

Allowed decisions:

1. source platform input
2. target member input
3. source member output for a platform output
4. target platform output
5. no conversion operation: representation adaptation belongs to runtime

member_to_member is not a planner decision. It is copied port-for-port from
the frozen ResponsibilityGraph and MUST NOT be selected or replaced by the model.

The planner must not generate a transform, select an adapter, choose a
serializer, or declare a conversion. Representation adaptation is handled by
runtime capability.
"""
VALID_INTERFACE_FREEZE_CONTRACT = """Interfaces without validation errors are immutable.

Repair MUST NOT:
- reorder them
- rename them
- change their source
- change their target

Repair may only modify interfaces listed in violations.
"""
PLATFORM_OUTPUT_CONTRACT = """PLATFORM OUTPUT CONTRACT

final_output_fields defines the legal platform-output domain.

Platform output field names are canonical contract identifiers, not natural
language descriptions.

The Interface Planner must select an existing platform output field exactly.
It must not rename, refine, generalize, specialize, or replace a platform output
field according to the content format, presentation style, or implementation
detail.

For example:
- a textual result does not create a new "markdown" platform output;
- a document-like result does not create a new "report" platform output;
- a generated artifact does not create a new output slot.

A FunctionItem output name and a platform output name belong to different
semantic layers. Their names may differ, but the target_platform_output must
always be selected from the declared platform output contract.

Every selected platform output must belong to final_output_fields.
"""
PLATFORM_BOUNDARY_CONTRACT = """PLATFORM BOUNDARY CONTRACT

Platform logical inputs are semantic values supplied by the host at or
before Skill invocation.

Platform logical outputs are semantic values returned by the Skill to the
host after Skill execution.

The platform boundary is an external boundary. It is not intermediate
storage, a relay, scratchpad, or message bus between FunctionItems.

Choose Interface kind from the actual semantic provenance:

- Use platform_to_member when the required semantic value is actually
  supplied by a legal platform input. source_path may select a nested
  semantic value from that platform input.

- Use member_to_member when the required semantic value is produced by
  one frozen FunctionItem and consumed by another frozen FunctionItem.

- Use member_to_platform when a FunctionItem-produced semantic value is
  intended to leave the Skill through a legal platform output.

Do not route a FunctionItem-produced intermediate semantic value through
the platform merely so another FunctionItem can consume it.

Do not choose Interface kind from field-name similarity."""

SOURCE_PROVENANCE_CONTRACT = """SOURCE PROVENANCE CONTRACT

A valid source must be able to provide the semantic value required by the
target.  Source existence, name similarity, and schema convertibility are not
evidence of that fact.  Never create a binding merely to close coverage.

Compatibility is evaluated from the declared source role, source origin, and
target role. Representation conversion is a runtime capability, not interface
planning information. An optional input with no valid source remains unbound. A derived
input accepts only a preceding FunctionItem output and never a platform input.
"""

INPUT_PORT_ROLES = frozenset({
    "required_runtime_input", "optional_runtime_input", "derived_input",
})
OUTPUT_PORT_ROLES = frozenset({"runtime_output", "intermediate_output"})
REVIEW_ERROR_TYPES = frozenset({
    "binding_error", "provenance_error", "missing_source_error",
    "schema_error",
})

PLATFORM_OUTPUT_MAPPING_CONTRACT = """PLATFORM OUTPUT MAPPING CONTRACT

FunctionItem outputs and platform outputs belong to different semantic layers.

FunctionItem outputs and platform outputs are different semantic layers.

Representation conversion is a runtime capability, not interface planning
information. The reviewer MUST NOT reject a mapping only because source and
target schemas differ.

The reviewer should judge whether semantic meaning is preserved, not require
structural schema equality.

FunctionItem outputs are internal logical ports produced inside the Skill.

Platform outputs are external boundary ports exposed to the host.

They are different namespaces.

A member output name MUST NOT automatically become a platform output name.

A member_to_platform Interface must satisfy:

FunctionItem output
        ->
existing platform output field


The planner MUST follow this order:

1. Identify the semantic value produced by source_member.source_output.

2. Read the declared platform output contract.

3. Select target_platform_output ONLY from the declared platform output fields.

4. Verify the selected platform output semantically represents the produced value.


Forbidden:

Do not copy source_output into target_platform_output only because names are identical.

Invalid example:

source_member:
scripts/main.py

source_output:
file_outputs

target_platform_output:
file_outputs


This is invalid unless the platform contract explicitly declares:

final_output_fields:
[
    "file_outputs"
]


Do not create new platform outputs because:
- the internal output is a file;
- the internal output is a report;
- the internal output is markdown;
- the internal output is a document;
- the internal output has a convenient name.

If no declared platform output can represent the internal result, the Interface Plan is invalid.

A different name does not imply incompatibility.

Valid example:

source_output:
internal_result

target_platform_output:
external_result


The reviewer evaluates semantic compatibility only after deterministic
validation has established that target_platform_output belongs to the declared
platform contract.

Interface planning only determines semantic connections.
The planner MUST NOT generate conversion operations.
The planner MUST NOT invent transform names.
A valid interface describes source port -> target port.
Representation conversion is handled by the runtime capability layer.
"""

RUNTIME_INPUT_PROVENANCE_CONTRACT = """RUNTIME INPUT CONTRACT BOUNDARY

A FunctionItem input is a reusable invocation contract.  It declares the input
name, type, whether it is required or optional, and (when appropriate) a local
default.  It does not declare where a future runtime obtains a user's value.

RUNTIME SLOT IDENTITY RULE

Each FunctionItem input declaration is an independent contract.

When an explicit member-to-member transfer exists, its target identity is:

(target_member, target_input)

not by input name or semantic value.

If multiple FunctionItems declare the same input name, they still have distinct
input contracts; this does not require either one to have a Creator-time
platform binding.

Creator must not require a FunctionItem input to be mapped to a current platform
input.  At invocation time runtime combines raw user input with this contract,
applies explicit defaults, and reports any still-missing required input."""
MULTIMODAL_INPUT_PROVENANCE_CONTRACT = """MULTIMODAL INPUT PROVENANCE CONTRACT

No platform input source is universally required.
For each FunctionItem receiving slot, independently determine the semantic
value required by that slot and where that value actually originates.
If it is the user's runtime free-form instruction, use the platform contract's
canonical free-form-request representation. If it is runtime-uploaded file or
multimodal content, use the runtime-file representation. Business inputs are
declared as independent top-level semantic source slots in the platform contract.
A FunctionItem may consume multiple different source families simultaneously.

Do not infer a wrapper or nested structure from a FunctionItem input's name or
shape. source_path only selects a nested value that already exists in the
declared platform input hierarchy; it never creates a new hierarchy or
container. Every platform source must already be declared by the current Skill
and must not be invented merely to close coverage.
If another FunctionItem produces the value, use member_to_member.
A FunctionItem may consume multiple different source families simultaneously.

INPUT SOURCE SEMANTICS ARE DESCRIPTIVE, NOT A CLOSED WHITELIST.
input_source_semantics describes canonical and related representations whose
relationships are known. input_envelope_fields remains the legal source domain;
the metadata does not remove or forbid another declared source. When confirmed
requirements establish that another legal platform source owns the value, it
may be selected. Do not invent an unknown platform source.

Interface Planner is the owner of semantic provenance decisions. The backend
only validates candidate domain, logical references, coverage, single
provenance, and graph materialization; it does not infer a business source.
A FunctionItem logical input name is a receiver-local interface identity and
does not redefine external provenance. Do not choose a platform source because
its name resembles the target input, and do not force user_request when another
source owns the value.

runtime_source_required=true requires provenance. When it is false, backend
coverage is not required, but optional does not mean forbidden: a valid binding
may remain when the Skill explicitly supports that optional runtime input."""
RUNTIME_BINDING_FACTS_CONTRACT = """RUNTIME BINDING FACTS CONTRACT

Runtime binding facts are authoritative.

The Interface Planner MUST NOT infer or invent runtime provenance.
For every FunctionItem input:
1. If runtime binding facts provide allowed sources, use only those sources.
2. If no valid source exists, leave the input unresolved.
3. Never bind another platform input only because names are similar.
4. Never use one platform input as a container for another logical input.

Forbidden: input_files -> primary_key_field
Correct: options.primary_key_field -> primary_key_field
"""

RUNTIME_REPAIR_RESTRICTION_CONTRACT = """RUNTIME REPAIR RESTRICTION CONTRACT

Repair is not allowed to redesign runtime provenance.

Repair MUST NOT:
- invent new platform input names
- replace source based on semantic similarity
- switch between input envelope fields

Repair MUST ONLY:
- remove invalid interface
- replace source using provided allowed_sources
- replace target using declared ports

All replacement sources MUST exist in runtime_binding_facts.
"""

SOURCE_PATH_CONTRACT = """SOURCE PATH CONTRACT

Every platform_to_member Interface MUST explicitly contain source_path.
source_platform_input identifies the selected top-level platform source slot.
source_path is relative to that selected top-level source slot. It only selects
a nested value that already exists inside the declared platform input; it never
creates a new input hierarchy.
Use source_path=[] when the entire selected top-level platform source value is transferred.
Use a non-empty source_path only when the semantic source value is nested inside the selected top-level platform source.
Never omit source_path.
Do not repeat source_platform_input inside source_path merely to satisfy the schema."""

PLATFORM_INPUT_HIERARCHY_CONTRACT = """PLATFORM INPUT HIERARCHY CONTRACT

Platform input hierarchy is frozen by the upstream Blueprint and runtime contract.

Platform inputs are flat semantic slots. Each declared top-level platform input
is an independent source slot. Bind a declared business input directly: for
example, source_platform_input="primary_key" with source_path=[] is valid when
primary_key is declared. Do not instead bind source_platform_input="fields" with
source_path=["primary_key"].

Interface Planner consumes the existing platform input structure.
It does not redesign, normalize, reorganize, or introduce a new input hierarchy.

A platform input binding must preserve the exact hierarchy declared by the
upstream platform contract.

For a top-level platform input:

- source_platform_input must reference that declared top-level input.
- source_path describes only a nested value inside that selected top-level input.
- source_path must not be used to create an implicit parent container or move
  the input into another namespace.

Do not infer additional nesting from:
- target FunctionItem input names
- semantic similarity between names
- common parameter grouping patterns
- expected convenience structures

Do not create synthetic containers such as fields, parameters, request, or
config unless that exact container is explicitly declared as a platform input.

Only use nested source paths when the upstream platform contract explicitly
declares that nested structure.

Examples:

Valid:

Platform contract declares:

input:
  user_parameter

Interface:

source_platform_input:
  user_parameter

source_path:
  []


Valid:

Platform contract declares:

input:
  request_context:
    user_parameter

Interface:

source_platform_input:
  request_context

source_path:
  [
    "user_parameter"
  ]


Invalid:

Platform contract declares:

input:
  user_parameter

Interface:

source_platform_input:
  request_context

source_path:
  [
    "user_parameter"
  ]

because request_context.user_parameter is not declared by the upstream
platform contract.

The Interface Planner may select existing platform sources and nested values,
but must not invent or transform the platform input hierarchy.
"""

REFINEMENT_FEEDBACK_CONTRACT = """REFINEMENT FEEDBACK CONTRACT

The previous candidate did not satisfy all acceptance facts. The backend reports
only observed acceptance facts and whether the previous candidate changed the
editable semantic state. These observations are not repair instructions.
If semantic_changed=false, independently reconsider the complete problem from
the authoritative facts instead of repeating the previous candidate. Every
supplied acceptance fact is a hard acceptance condition. Return one complete candidate.

PREVIOUS CANDIDATE AUTHORITY
The previous candidate is not an authoritative fact. It is only a prior proposal
that failed acceptance. Preserve it only where consistent with confirmed
requirements, frozen contracts, and current acceptance facts. Acceptance facts
win over the previous candidate. Minimize edits only among candidates that fully
satisfy all acceptance facts. Never preserve invalid or incomplete semantic state
merely to minimize changes."""
INTERFACE_KINDS = {"platform_to_member", "member_to_member", "member_to_platform"}
# Transform is a runtime capability, not interface planning information.



REPAIRABLE_INTERFACE_ISSUES = frozenset({
    "missing_source_binding",
    "missing_target_binding",
    "invalid_source_port",
    "invalid_target_port",
})
_DANGEROUS_PATH_PARTS = {"__proto__", "prototype", "constructor"}
INTERFACE_FIELDS = {
    "platform_to_member": {"interface_id", "kind", "source_platform_input", "source_path", "target_member", "target_input"},
    "member_to_member": {"interface_id", "kind", "source_member", "source_output", "target_member", "target_input"},
    "member_to_platform": {"interface_id", "kind", "source_member", "source_output", "target_platform_output"},
}
PLATFORM_INPUT_SOURCE_TYPE = "platform_input"
# ``goal`` is accepted only as a persisted-contract compatibility alias. New
# contracts and the published schema use ``semantic_reason``.
OPTIONAL_INTERFACE_FIELDS = {kind: {"semantic_reason", "goal"} for kind in INTERFACE_KINDS}
OPTIONAL_INTERFACE_FIELDS["platform_to_member"].add("source_type")
INTERFACE_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["interfaces"],
    "properties": {"interfaces": {"type": "array", "items": {"oneOf": [
        {"type": "object", "additionalProperties": False,
         "required": sorted(fields | ({"source_type"} if kind == "platform_to_member" else set())),
         "properties": {
             **{key: ({"const": kind} if key == "kind" else {"type": "array", "items": {"type": "string", "minLength": 1}} if key == "source_path" else {"type": "string", "minLength": 1}) for key in fields},
             **({"source_type": {"const": PLATFORM_INPUT_SOURCE_TYPE}} if kind == "platform_to_member" else {}),
             "semantic_reason": {"type": "string", "minLength": 1},
         }}
        for kind, fields in INTERFACE_FIELDS.items()
    ]}}},
}

INTERFACE_PATCH_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["operations"],
    "properties": {"operations": {"type": "array", "items": {
        "type": "object", "required": ["op", "interface_id", "reason"],
        "properties": {
            "op": {"enum": ["remove_interface", "replace_source", "replace_target"]},
            "interface_id": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1, "maxLength": 200},
            "source_platform_input": {"type": "string", "minLength": 1},
            "source_path": {"type": "array", "items": {"type": "string"}},
            "source_member": {"type": "string", "minLength": 1},
            "source_output": {"type": "string", "minLength": 1},
            "target_member": {"type": "string", "minLength": 1},
            "target_input": {"type": "string", "minLength": 1},
            "target_platform_output": {"type": "string", "minLength": 1},
        }, "additionalProperties": False,
    }}},
}


def validate_patch_operation_against_runtime_binding_facts(
    *, operation: dict[str, Any], interface: dict[str, Any],
    runtime_binding_facts: dict[str, Any],
) -> None:
    """Reject a platform source edit that is not present in frozen facts."""
    if operation.get("op") != "replace_source" or "source_platform_input" not in operation:
        return
    observed = {
        "source_platform_input": operation.get("source_platform_input"),
        "source_path": list(operation.get("source_path") or []),
    }
    member = interface.get("target_member")
    slot = interface.get("target_input")
    allowed = (
        runtime_binding_facts.get(member, {}).get(slot, {}).get("allowed_sources") or []
    )
    if observed not in allowed:
        raise InterfaceIntentPlanError(
            "replacement source is absent from frozen runtime binding facts",
            code="invalid_interface_patch_source",
            details={"interface_id": interface.get("interface_id"), "target": f"{member}.{slot}",
                     "observed_source": observed, "allowed_sources": allowed},
        )


def validate_interface_patch_protocol(patch: Any) -> dict[str, Any]:
    """Validate the deliberately small, strict repair wire protocol."""
    if not isinstance(patch, dict) or set(patch) != {"operations"} or not isinstance(patch["operations"], list):
        raise InterfaceIntentPlanError("repair must be patch operations", code="invalid_interface_patch", details={"path": "$"})
    common = {"op", "interface_id", "reason"}
    for index, operation in enumerate(patch["operations"]):
        path = f"$.operations[{index}]"
        if not isinstance(operation, dict) or not common <= set(operation):
            raise InterfaceIntentPlanError("patch operation fields are invalid", code="invalid_interface_patch", details={"path": path})
        op = operation.get("op")
        if op not in {"remove_interface", "replace_source", "replace_target"}:
            raise InterfaceIntentPlanError("patch operation is unsupported", code="invalid_interface_patch", details={"path": f"{path}.op"})
        allowed_fields = {
            "remove_interface": common,
            "replace_source": common | {"source_platform_input", "source_path", "source_member", "source_output"},
            "replace_target": common | {"target_member", "target_input", "target_platform_output"},
        }[op]
        if not set(operation) <= allowed_fields:
            raise InterfaceIntentPlanError("patch operation contains fields invalid for op", code="invalid_interface_patch", details={"path": path})
        if op == "replace_source":
            platform_source = "source_platform_input" in operation and "source_path" in operation
            member_source = "source_member" in operation and "source_output" in operation
            if platform_source == member_source:
                raise InterfaceIntentPlanError("replace_source must contain exactly one source shape", code="invalid_interface_patch", details={"path": path})
        if op == "replace_target":
            member_target = "target_member" in operation and "target_input" in operation
            platform_target = "target_platform_output" in operation
            if member_target == platform_target:
                raise InterfaceIntentPlanError("replace_target must contain exactly one target shape", code="invalid_interface_patch", details={"path": path})
        if not isinstance(operation.get("interface_id"), str) or not operation["interface_id"].strip():
            raise InterfaceIntentPlanError("patch interface_id is invalid", code="invalid_interface_patch", details={"path": f"{path}.interface_id"})
        reason = operation.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise InterfaceIntentPlanError("patch reason must contain at most 200 characters", code="invalid_interface_patch", details={"path": f"{path}.reason"})
    return patch


def apply_interface_patch(
    plan: Any, patch: Any, *, runtime_binding_facts: dict[str, Any] | None = None,
) -> Any:
    """Apply the bounded Interface repair protocol without regenerating a plan."""
    legacy_list = isinstance(plan, list)
    interfaces = [dict(value) for value in (plan if legacy_list else plan.get("interfaces") or [])]
    if (isinstance(patch, dict)
            and all("op" in value for value in (patch.get("operations") or []))):
        patch = validate_interface_patch_protocol(patch)
    operations = patch if isinstance(patch, list) else patch.get("operations") or []
    by_id = {value.get("interface_id"): value for value in interfaces}
    for operation in operations:
        # Keep compatibility with the former local test helper's add/modify form.
        if operation.get("action") == "modify" and operation.get("interface_id") in by_id:
            by_id[operation["interface_id"]].update(operation.get("changes") or {})
            continue
        if operation.get("action") == "add" and isinstance(operation.get("interface"), dict):
            value = dict(operation["interface"]); interfaces.append(value); by_id[value.get("interface_id")] = value
            continue
        iid, op = operation.get("interface_id"), operation.get("op")
        if iid not in by_id:
            raise InterfaceIntentPlanError("patch references unknown interface", code="invalid_interface_patch", details={"interface_id": iid})
        current = by_id[iid]
        if op == "remove_interface":
            interfaces.remove(current); del by_id[iid]
        elif op == "replace_source":
            if "source_platform_input" in operation:
                for key in ("source_member", "source_output"): current.pop(key, None)
                current.update(kind="platform_to_member", source_platform_input=operation["source_platform_input"], source_path=list(operation.get("source_path") or []))
            elif "source_member" in operation and "source_output" in operation:
                current.pop("source_platform_input", None); current.pop("source_path", None)
                current.update(kind="member_to_member", source_member=operation["source_member"], source_output=operation["source_output"])
            else:
                raise InterfaceIntentPlanError("replace_source lacks source fields", code="invalid_interface_patch", details={"interface_id": iid})
        elif op == "replace_target":
            if "target_platform_output" in operation:
                current.pop("target_member", None); current.pop("target_input", None)
                current.update(kind="member_to_platform", target_platform_output=operation["target_platform_output"])
            else:
                current.pop("target_platform_output", None)
                current.update(target_member=operation["target_member"], target_input=operation["target_input"])
        else:
            raise InterfaceIntentPlanError("unsupported interface patch operation", code="invalid_interface_patch", details={"op": op})
    return interfaces if legacy_list else {"interfaces": interfaces}


def validate_interface_patch(
    *, plan: dict[str, Any], patch: dict[str, Any], function_items: list[dict[str, Any]],
    platform_contract: dict[str, Any] | None = None,
    violations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate and apply an edge patch atomically against frozen endpoint slots."""
    patch = validate_interface_patch_protocol(patch)
    before = {str(v.get("interface_id") or ""): v for v in plan.get("interfaces") or []}
    editable = {str(v.get("interface_id") or "") for v in (violations or []) if v.get("interface_id")}
    for operation in patch["operations"]:
        iid = operation["interface_id"]
        if iid not in before:
            raise InterfaceIntentPlanError("patch references unknown interface", code="invalid_interface_patch", details={"interface_id": iid})
        if violations is not None and iid not in editable:
            raise InterfaceIntentPlanError("patch modifies a frozen valid interface", code="invalid_interface_patch_frozen_interface", details={"interface_id": iid})
    facts = build_runtime_binding_facts(function_items=function_items, platform_contract=platform_contract)
    original_interface = plan
    candidate = apply_interface_patch(plan, patch, runtime_binding_facts=facts)
    patched_interface = candidate
    if original_interface == patched_interface:
        raise InterfaceIntentPlanError(
            "patch does not change the Interface Plan",
            code="no_effective_patch", details={"path": "$.operations"},
        )
    # Protocol/reference validation proves both source and target are existing
    # frozen ports before any candidate can enter refinement acceptance.
    validate_interface_intent_plan(plan=candidate, function_items=function_items)
    blocking = collect_interface_plan_validation_issues(
        plan=candidate, function_items=function_items, platform_contract=platform_contract,
    )
    if blocking:
        raise InterfaceIntentPlanError(
            "interface patch does not preserve deterministic closure",
            code="invalid_interface_patch_closure", details={"issues": blocking},
        )
    return candidate


CANONICAL_INTERFACE_CONTRACT_RULE = """CANONICAL INTERFACE CONTRACT

The Interface Plan is the single source of truth for executable interface
binding.  If an Interface Plan exists, every later generation stage MUST use it
directly.  Do NOT infer interface bindings from graph node names, blueprint
descriptions, or FunctionItem/function descriptions.

The canonical projection of every binding contains direction, source
(kind/field/schema), target (kind/field), A dotted source field
is a real path declared by the platform input contract; words such as options,
config, and params have no special meaning and are neither forbidden nor
implicitly inserted.  Generated command variables and script argv keys equal
target.field, while runtime lookup paths equal source.field.
"""


def build_canonical_interface_contract(
    *, plan: dict[str, Any], function_items: list[dict[str, Any]],
    platform_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project the accepted legacy wire shape into the sole executable contract.

    The planner wire format retains endpoint member identifiers needed by graph
    materialization.  This projection deliberately contains only binding facts
    consumed by generators, preventing those stages from reconstructing paths,
    names or schemas from descriptive text.
    """
    validated = validate_interface_intent_plan(plan=plan, function_items=function_items)
    compact = _compact_function_items(function_items)
    schemas = {
        (item["target_file"], port["name"]): dict(port.get("contract") or {})
        for item in compact for port in item.get("outputs") or []
    }
    output_roles = {
        (item["target_file"], port["name"]): port.get("role")
        for item in compact for port in item.get("outputs") or []
    }
    input_contracts = {
        (item["target_file"], port["name"]): port
        for item in compact for port in item.get("inputs") or []
    }
    boundary = (platform_contract or {}).get("platform_skill_boundary", platform_contract or {})
    platform_input_schemas = boundary.get("input_schemas") if isinstance(boundary.get("input_schemas"), dict) else {}
    interfaces: list[dict[str, Any]] = []
    for interface in validated["interfaces"]:
        kind = interface["kind"]
        if kind == "platform_to_member":
            path = [interface["source_platform_input"], *interface["source_path"]]
            source = {
                "kind": "platform_input",
                "role": "platform_input",
                "field": ".".join(path),
                "schema": dict(platform_input_schemas.get(interface["source_platform_input"]) or {}),
            }
            target_port = input_contracts.get((interface["target_member"], interface["target_input"]), {})
            target = {"kind": "function_input", "field": interface["target_input"],
                      "role": target_port.get("role"), "schema": dict(target_port.get("contract") or {})}
            direction = "input"
        elif kind == "member_to_member":
            source = {
                "kind": "function_output",
                "role": output_roles.get((interface["source_member"], interface["source_output"])),
                "field": interface["source_output"],
                "schema": schemas.get((interface["source_member"], interface["source_output"]), {}),
            }
            target_port = input_contracts.get((interface["target_member"], interface["target_input"]), {})
            target = {"kind": "function_input", "field": interface["target_input"],
                      "role": target_port.get("role"), "schema": dict(target_port.get("contract") or {})}
            direction = "input"
        else:
            source = {
                "kind": "function_output",
                "role": output_roles.get((interface["source_member"], interface["source_output"])),
                "field": interface["source_output"],
                "schema": schemas.get((interface["source_member"], interface["source_output"]), {}),
            }
            target = {"kind": "platform_output", "field": interface["target_platform_output"]}
            direction = "output"
        runtime_provenance = None
        if kind == "platform_to_member":
            runtime_provenance = {
                "source_type": "platform_input",
                "source_platform_input": interface["source_platform_input"],
                "source_path": list(interface["source_path"]),
            }
        interfaces.append({
            "interface_id": interface["interface_id"],
            "direction": direction,
            "source": source,
            "target": target,
            "runtime_provenance": runtime_provenance,
            **({"semantic_reason": interface.get("semantic_reason") or interface.get("goal")}
               if interface.get("semantic_reason") or interface.get("goal") else {}),
        })
    return {"interfaces": interfaces}


def _script_arg_paths(source: str) -> set[str]:
    """Return literal ``args`` access paths without applying naming policy."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    paths: set[str] = set()
    for node in ast.walk(tree):
        parts: list[str] = []
        current: ast.AST = node
        while isinstance(current, ast.Subscript) and isinstance(current.slice, ast.Constant) and isinstance(current.slice.value, str):
            parts.append(current.slice.value)
            current = current.value
        if isinstance(current, ast.Name) and current.id == "args" and parts:
            paths.add(".".join(reversed(parts)))
    return paths


def collect_interface_contract_consistency_issues(
    *, interface_contract: dict[str, Any], script_sources: dict[str, str] | None = None,
    command_variables: dict[str, list[str]] | None = None,
    runtime_bindings: dict[str, list[str]] | None = None,
    artifact_contracts: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Deterministically reject downstream bindings that diverge from the plan."""
    issues: list[dict[str, Any]] = []
    scripts = script_sources or {}
    commands = command_variables or {}
    bindings = runtime_bindings or {}
    for artifact, observed_contract in (artifact_contracts or {}).items():
        if observed_contract != interface_contract:
            issues.append({"code": "interface_contract_mismatch", "error_type": "schema_error",
                           "artifact": artifact, "observed": observed_contract,
                           "expected": interface_contract})
    for interface in interface_contract.get("interfaces") or []:
        iid = str(interface.get("interface_id") or "")
        target = str((interface.get("target") or {}).get("field") or "")
        source = str((interface.get("source") or {}).get("field") or "")
        for artifact, observed in (
            ("SKILL command", set(commands.get(iid) or [])),
            ("script", _script_arg_paths(scripts.get(iid, ""))),
        ):
            if observed and target not in observed:
                issues.append({"code": "interface_target_field_mismatch", "interface_id": iid,
                               "artifact": artifact, "observed": sorted(observed), "expected": target})
        observed_bindings = set(bindings.get(iid) or [])
        if observed_bindings and source not in observed_bindings:
            issues.append({"code": "interface_source_field_mismatch", "interface_id": iid,
                           "artifact": "runtime binding", "observed": sorted(observed_bindings), "expected": source})
    return issues


def _include_previous_interface_plan(feedback: dict[str, Any]) -> bool:
    return feedback["progress"]["semantic_changed"] is not False



class InterfaceIntentPlanError(GraphValidationError):
    """Machine-readable Interface Intent Plan failure."""



def _strip_single_json_fence(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[0].strip().lower() not in {"```", "```json"} or lines[-1].strip() != "```":
        return stripped
    return "\n".join(lines[1:-1]).strip()


def _parse_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(_strip_single_json_fence(text))
    except (TypeError, json.JSONDecodeError) as exc:
        raise InterfaceIntentPlanError("interface plan must be strict JSON", code="invalid_interface_plan_json", details={"path": "$"}) from exc
    if not isinstance(value, dict):
        raise InterfaceIntentPlanError("interface plan must be a JSON object", code="invalid_interface_plan_protocol", details={"path": "$"})
    return value


def _require_nonempty_string(value: dict[str, Any], key: str, code: str, path: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise InterfaceIntentPlanError(f"{key} must be a non-empty string", code=code, details={"path": path})
    return raw.strip()


def _raise(message: str, code: str, *, path: str, **details: Any) -> None:
    payload = {"path": path, **{k: v for k, v in details.items() if v is not None}}
    raise InterfaceIntentPlanError(message, code=code, details=payload)


def _compact_port_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(
            value.get("port_id")
            or value.get("id")
            or value.get("name")
            or value.get("field")
            or ""
        ).strip()
    return str(value or "").strip()


def required_platform_output_fields(platform_contract: dict[str, Any] | None) -> set[str]:
    """Return only explicitly declared required platform outputs."""
    contract = platform_contract or {}
    boundary = contract.get("platform_skill_boundary", contract)
    if not isinstance(boundary, dict) or "required_final_output_fields" not in boundary:
        return set()
    raw = boundary.get("required_final_output_fields")
    return {_compact_port_id(value) for value in raw if _compact_port_id(value)} if isinstance(raw, list) else set()


def runtime_input_source_facts(raw_input: Any, default_values: dict[str, Any] | None = None) -> dict[str, bool]:
    """Return the single deterministic required/default interpretation.

    This deliberately answers only whether a runtime source is necessary; it
    never selects that source.
    """
    port_id = _compact_port_id(raw_input)
    defaults = default_values if isinstance(default_values, dict) else {}
    inline_default = isinstance(raw_input, dict) and "default" in raw_input
    # ``options.font`` is a field default owned by the declared structured
    # ``options`` input; it is not a second logical port.
    default_present = inline_default or (
        bool(port_id)
        and any(key == port_id or key.startswith(f"{port_id}.") for key in defaults)
    )
    explicitly_optional = isinstance(raw_input, dict) and raw_input.get("required") is False
    return {
        "required": not explicitly_optional,
        "default_present": default_present,
        "runtime_source_required": not explicitly_optional and not default_present,
    }


def _input_port_role(raw_input: Any, facts: dict[str, bool]) -> str:
    """Return the explicit role, with a compatibility default for old plans."""
    role = str(raw_input.get("role") or "").strip() if isinstance(raw_input, dict) else ""
    if role and role not in INPUT_PORT_ROLES:
        raise ValueError(f"invalid FunctionItem input role: {role}")
    if role:
        return role
    return "required_runtime_input" if facts["runtime_source_required"] else "optional_runtime_input"


def _output_port_role(raw_output: Any) -> str:
    """Return the explicit output role, retaining legacy plan compatibility."""
    role = str(raw_output.get("role") or "").strip() if isinstance(raw_output, dict) else ""
    if role and role not in OUTPUT_PORT_ROLES:
        raise ValueError(f"invalid FunctionItem output role: {role}")
    return role or "runtime_output"


def _compact_function_items(function_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = normalize_structured_function_items(function_items, source="interface_intent_plan")
    compact_items: list[dict[str, Any]] = []
    for item in normalized:
        default_values = item.get("default_values") if isinstance(item.get("default_values"), dict) else {}
        raw_inputs = item.get("inputs") or []
        input_ids = [_compact_port_id(raw_input) for raw_input in raw_inputs]
        compact_inputs = []
        for raw_input, port_id in zip(raw_inputs, input_ids):
            if not port_id:
                continue
            facts = runtime_input_source_facts(raw_input, default_values)
            description = str(raw_input.get("description") or "") if isinstance(raw_input, dict) else ""
            contract = dict(raw_input.get("contract") or {}) if isinstance(raw_input, dict) and isinstance(raw_input.get("contract"), dict) else {}
            role = _input_port_role(raw_input, facts)
            compact_input = {
                "name": port_id,
                "role": role,
                "description": description,
                "contract": contract,
                "required": facts["required"],
                "optional": not facts["required"],
            }
            if facts["default_present"]:
                if isinstance(raw_input, dict) and "default" in raw_input:
                    compact_input["default"] = raw_input.get("default")
                elif port_id in default_values:
                    compact_input["default"] = default_values[port_id]
            compact_inputs.append(compact_input)
        required_inputs = [
            value["name"] for value in compact_inputs
            if value["required"]
        ]
        defaulted_inputs = [value["name"] for value in compact_inputs if "default" in value]
        compact_items.append(
            {
                "target_file": item["target_file"],
                "purpose": item.get("purpose", ""),
                "inputs": compact_inputs,
                "outputs": [
                    {"name": _compact_port_id(value),
                     "role": _output_port_role(value),
                     "description": str(value.get("description") or "") if isinstance(value, dict) else "",
                     "contract": dict(value.get("contract") or {}) if isinstance(value, dict) and isinstance(value.get("contract"), dict) else {}}
                    for value in item.get("outputs") or [] if _compact_port_id(value)
                ],
                "required_inputs": required_inputs,
                "defaulted_inputs": defaulted_inputs,
            }
        )
    return compact_items


def build_frozen_interface_slots(
    *, function_items: list[dict[str, Any]], platform_contract: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project the frozen contracts into the only endpoint domain a planner may use."""
    compact = _compact_function_items(function_items)
    boundary = (platform_contract or {}).get("platform_skill_boundary", platform_contract or {})
    input_schemas = boundary.get("input_schemas") if isinstance(boundary.get("input_schemas"), dict) else {}
    output_sinks = boundary.get("output_sinks") if isinstance(boundary.get("output_sinks"), dict) else {}
    return {
        "members": {
            item["target_file"]: {
                "inputs": [{"name": port["name"], "type": _schema_type(port["contract"])} for port in item["inputs"]],
                "outputs": [{"name": port["name"], "type": _schema_type(port["contract"])} for port in item["outputs"]],
            }
            for item in compact
        },
        "platform_ports": {
            "inputs": [
                {"name": name, "type": _schema_type(dict(input_schemas.get(name) or {}))}
                for name in sorted({_compact_port_id(value) for value in boundary.get("input_envelope_fields") or []})
            ],
            "outputs": [
                {"name": name, "type": (output_sinks.get(name) or {}).get("semantic_type")}
                for name in sorted(platform_output_names(platform_contract))
            ],
        },
    }


def runtime_slot_key(member: str, slot_name: str) -> tuple[str, str]:
    """Return the single canonical identity for a runtime receiving slot."""
    return (str(member), str(slot_name))


def validate_interface_plan_protocol(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize only the wire protocol, not member semantics."""
    if not isinstance(plan, dict) or set(plan) != {"interfaces"}:
        _raise("interface plan must contain only interfaces", "invalid_interface_plan_protocol", path="$")
    interfaces = plan.get("interfaces")
    if not isinstance(interfaces, list):
        _raise("interfaces must be a list", "invalid_interface_plan_protocol", path="$.interfaces")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(interfaces):
        path = f"$.interfaces[{index}]"
        if not isinstance(raw, dict):
            _raise("interface must be an object", "invalid_interface_protocol", path=path)
        kind = raw.get("kind")
        if kind not in INTERFACE_KINDS:
            _raise("interface kind is invalid", "invalid_interface_kind", path=f"{path}.kind")
        expected = INTERFACE_FIELDS[kind]
        allowed = expected | OPTIONAL_INTERFACE_FIELDS[kind] | ({"source_type"} if kind == "platform_to_member" else set())
        if not expected <= set(raw) <= allowed:
            _raise("interface fields do not match kind schema", "invalid_interface_protocol", path=path,
                   expected=sorted(expected), observed=sorted(raw))
        interface_id = _require_nonempty_string(raw, "interface_id", "invalid_interface_protocol", f"{path}.interface_id")
        if interface_id in seen:
            _raise("duplicate interface_id", "duplicate_interface_id", path=f"{path}.interface_id")
        seen.add(interface_id)
        if "semantic_reason" in raw:
            _require_nonempty_string(raw, "semantic_reason", "invalid_interface_protocol", f"{path}.semantic_reason")
        if "goal" in raw:
            _require_nonempty_string(raw, "goal", "invalid_interface_protocol", f"{path}.goal")
        for field in expected - {"interface_id", "kind", "source_path"}:
            _require_nonempty_string(raw, field, "invalid_interface_protocol", f"{path}.{field}")
        if kind == "platform_to_member":
            if raw.get("source_type", PLATFORM_INPUT_SOURCE_TYPE) != PLATFORM_INPUT_SOURCE_TYPE:
                _raise("source_type must be platform_input", "invalid_interface_protocol", path=f"{path}.source_type")
            source_path = raw.get("source_path")
            if not isinstance(source_path, list) or any(not isinstance(part, str) or not part or part.lower() in _DANGEROUS_PATH_PARTS for part in source_path):
                _raise("source_path must contain only safe non-empty strings", "invalid_interface_protocol", path=f"{path}.source_path")
        value = dict(raw)
        if kind == "platform_to_member":
            value["source_type"] = PLATFORM_INPUT_SOURCE_TYPE
        normalized.append(value)
    return {"interfaces": normalized}


def validate_interface_intent_plan(*, plan: dict[str, Any], function_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate interface-intent protocol and references only."""
    plan = validate_interface_plan_protocol(plan)
    if not isinstance(plan, dict) or set(plan) != {"interfaces"}:
        _raise("interface plan must contain only interfaces", "invalid_interface_plan_protocol", path="$")
    interfaces = plan.get("interfaces")
    if not isinstance(interfaces, list):
        _raise("interfaces must be a list", "invalid_interface_plan_protocol", path="$.interfaces")
    frozen_items = normalize_structured_function_items(function_items, source="interface_intent_plan")
    frozen_targets = {item["target_file"] for item in frozen_items}
    inputs_by_member = {item["target_file"]: {_compact_port_id(value) for value in item.get("inputs") or []} for item in frozen_items}
    outputs_by_member = {item["target_file"]: {_compact_port_id(value) for value in item.get("outputs") or []} for item in frozen_items}
    seen_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    counts = {"platform_to_member": 0, "member_to_member": 0, "member_to_platform": 0}
    for index, raw_interface in enumerate(interfaces):
        path = f"$.interfaces[{index}]"
        if not isinstance(raw_interface, dict):
            _raise("interface must be an object", "invalid_interface_protocol", path=path)
        kind = raw_interface.get("kind")
        if kind not in INTERFACE_KINDS:
            _raise("interface kind is invalid", "invalid_interface_kind", path=f"{path}.kind")
        expected = INTERFACE_FIELDS[kind]
        if not expected <= set(raw_interface) <= expected | OPTIONAL_INTERFACE_FIELDS[kind]:
            _raise("interface fields do not match kind schema", "invalid_interface_protocol", path=path, expected=sorted(expected), observed=sorted(raw_interface))
        interface_id = _require_nonempty_string(raw_interface, "interface_id", "invalid_interface_protocol", f"{path}.interface_id")
        if interface_id in seen_ids:
            _raise("duplicate interface_id", "duplicate_interface_id", path=f"{path}.interface_id", interface_id=interface_id)
        seen_ids.add(interface_id)
        if "semantic_reason" in raw_interface:
            _require_nonempty_string(raw_interface, "semantic_reason", "invalid_interface_protocol", f"{path}.semantic_reason")
        if "goal" in raw_interface:
            _require_nonempty_string(raw_interface, "goal", "invalid_interface_protocol", f"{path}.goal")
        interface = dict(raw_interface)
        if kind in {"member_to_member", "member_to_platform"}:
            source = _require_nonempty_string(raw_interface, "source_member", "invalid_interface_member", f"{path}.source_member")
            if source not in frozen_targets:
                _raise("source_member must reference a frozen FunctionItem", "unknown_interface_member", path=f"{path}.source_member", target=source)
            source_output = interface["source_output"]
            if source_output not in outputs_by_member[source]:
                _raise("source_output must reference a declared logical output", "unknown_interface_logical_port", path=f"{path}.source_output", target=source_output)
        if kind in {"platform_to_member", "member_to_member"}:
            target = _require_nonempty_string(raw_interface, "target_member", "invalid_interface_member", f"{path}.target_member")
            if target not in frozen_targets:
                _raise("target_member must reference a frozen FunctionItem", "unknown_interface_member", path=f"{path}.target_member", target=target)
            target_input = interface["target_input"]
            if target_input not in inputs_by_member[target]:
                _raise("target_input must reference a declared logical input", "unknown_interface_logical_port", path=f"{path}.target_input", target=target_input)
        if kind == "member_to_member" and interface["source_member"] == interface["target_member"]:
            _raise("member_to_member self connection is forbidden", "interface_self_connection", path=path, target=interface["source_member"])
        counts[kind] += 1
        normalized.append(interface)
        logger.info(
            "[Creator][interface_binding] interface_id=%s kind=%s source_logical_ref=%s target_logical_ref=%s",
            interface_id, kind,
            interface.get("source_output") or interface.get("source_platform_input"),
            interface.get("target_input") or interface.get("target_platform_output"),
        )
    logger.info(
        "[Creator][interface_plan] interface_count=%d platform_input_interface_count=%d member_interface_count=%d platform_output_interface_count=%d",
        len(normalized), counts["platform_to_member"], counts["member_to_member"], counts["member_to_platform"],
    )
    return {"interfaces": normalized}

def build_runtime_binding_facts(
    *, function_items: list[dict[str, Any]], platform_contract: dict[str, Any] | None,
) -> dict[str, dict[str, dict[str, list[dict[str, Any]]]]]:
    """Expose only explicitly frozen provenance; never plan mappings by type."""
    compact = _compact_function_items(function_items)
    boundary = (platform_contract or {}).get("platform_skill_boundary", platform_contract or {})
    names = [_compact_port_id(value) for value in boundary.get("input_envelope_fields") or []]
    facts: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    for item in compact:
        member_facts: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for port in item["inputs"]:
            contract = port.get("contract") or {}
            explicit = contract.get("runtime_provenance") if isinstance(contract.get("runtime_provenance"), dict) else contract
            explicit_source = explicit.get("source_platform_input")
            explicit_path = explicit.get("source_path", [])
            allowed: list[dict[str, Any]] = []
            if explicit_source in names and isinstance(explicit_path, list) and all(isinstance(x, str) for x in explicit_path):
                allowed.append({"source_platform_input": explicit_source, "source_path": list(explicit_path)})
            member_facts[port["name"]] = {"allowed_sources": allowed}
        facts[item["target_file"]] = member_facts
    logger.info("[interface_runtime_binding_facts] %s", json.dumps(facts, ensure_ascii=False, sort_keys=True))
    return facts


def validate_runtime_binding(
    *, interface: dict[str, Any], binding_facts: dict[str, Any],
) -> tuple[bool, str]:
    """Validate one platform binding against its frozen provenance projection."""
    if interface.get("kind") != "platform_to_member":
        return True, "non-platform binding"
    slot_facts = binding_facts.get(interface.get("target_member"), {}).get(interface.get("target_input"), {})
    allowed = slot_facts.get("allowed_sources") or []
    observed = {"source_platform_input": interface.get("source_platform_input"),
                "source_path": list(interface.get("source_path") or [])}
    return (observed in allowed, "binding is frozen" if observed in allowed else "source is absent from frozen runtime binding facts")


def _resolve_declared_source_path(schema: dict[str, Any], path: list[str]) -> dict[str, Any] | None:
    """Resolve a planner-selected path without assigning semantic meaning to types."""
    current = schema
    for part in path:
        properties = current.get("properties") if isinstance(current, dict) else None
        if isinstance(properties, dict) and isinstance(properties.get(part), dict):
            current = properties[part]
            continue
        raw_type = str(current.get("type") or "").lower() if isinstance(current, dict) else ""
        if part.isdigit() and raw_type in {"array", "list"} and isinstance(current.get("items"), dict):
            current = current["items"]
            continue
        if part.isdigit() and raw_type.startswith("list[") and raw_type.endswith("]"):
            current = {"type": raw_type[5:-1]}
            continue
        return None
    return current


def generate_provenance_candidates(
    *,
    target_member: str,
    target_input: str,
    compact_function_items: list[dict[str, Any]],
    platform_inputs: set[str],
) -> list[dict[str, Any]]:
    """
    Generate possible semantic provenance candidates.

    This function only discovers possible sources.
    It never decides the final Interface.

    Semantic selection remains owned by Interface Planner.
    """

    candidates: list[dict[str, Any]] = []

    # Candidate 1:
    # external platform inputs
    for source in sorted(platform_inputs):
        candidates.append(
            {
                "kind": "platform_to_member",
                "source_platform_input": source,
                "source_path": [],
                "target_member": target_member,
                "target_input": target_input,
                "evidence": "declared platform input source",
            }
        )

    return candidates


def build_semantic_mapping_candidates(
    *, function_items: list[dict[str, Any]], platform_contract: dict[str, Any] | None,
    responsibility_edges: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Enumerate boundary endpoints only; graph-owned inputs never compete."""
    compact = _compact_function_items(function_items)
    boundary = (platform_contract or {}).get("platform_skill_boundary", platform_contract or {})
    platform_inputs = {_compact_port_id(value) for value in boundary.get("input_envelope_fields") or []}
    platform_input_schemas = boundary.get("input_schemas") if isinstance(boundary.get("input_schemas"), dict) else {}
    platform_output_sinks = boundary.get("output_sinks") if isinstance(boundary.get("output_sinks"), dict) else {}
    input_candidates: list[dict[str, Any]] = []
    locked_inputs = set(_member_dependency_bindings(responsibility_edges))
    for item in compact:
        for port in item["inputs"]:
            if (item["target_file"], port["name"]) in locked_inputs:
                continue
            candidates = generate_provenance_candidates(
                target_member=item["target_file"], target_input=port["name"],
                compact_function_items=compact, platform_inputs=platform_inputs,
            )
            for candidate in candidates:
                if candidate["kind"] == "platform_to_member":
                    source_contract = dict(platform_input_schemas.get(candidate["source_platform_input"]) or {})
                else:
                    source_item = next(value for value in compact if value["target_file"] == candidate["source_member"])
                    source_port = next(value for value in source_item["outputs"] if value["name"] == candidate["source_output"])
                    source_contract = dict(source_port.get("contract") or {})
                candidate["source_contract"] = source_contract
                candidate["source_capabilities"] = _semantic_source_capabilities(source_contract)
                candidate["target_contract"] = dict(port.get("contract") or {})
            input_candidates.extend(candidates)
    output_candidates = [
        {"source_member": item["target_file"], "source_output": port["name"],
         "target_platform_output": target,
         "source_contract": dict(port.get("contract") or {}),
         "target_contract": dict((platform_output_sinks.get(target) or {}).get("value_schema") or {})}
        for item in compact for port in item["outputs"]
        for target in sorted(platform_output_names(platform_contract))
    ]
    return {"input_mappings": input_candidates, "output_mappings": output_candidates}


def _member_dependency_bindings(
    responsibility_edges: list[dict[str, Any]] | None,
) -> dict[tuple[str, str], tuple[str, str]]:
    """Project frozen member graph edges into receiving-port provenance."""
    bindings: dict[tuple[str, str], tuple[str, str]] = {}
    for edge in responsibility_edges or []:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("from_node") or edge.get("producer") or "").strip()
        source_port = str(edge.get("from_output") or edge.get("producer_port") or "").strip()
        target = str(edge.get("to_node") or edge.get("consumer") or "").strip()
        target_port = str(edge.get("to_input") or edge.get("consumer_port") or "").strip()
        if not all((source, source_port, target, target_port)):
            continue
        if source in {"platform_input_node", "platform"} or target in {"platform_output_node", "platform"}:
            continue
        key, value = (target, target_port), (source, source_port)
        if key in bindings and bindings[key] != value:
            _raise("graph assigns multiple sources to one input", "conflicting_graph_input_sources",
                   path="$.responsibility_edges", target_member=target, target_input=target_port)
        bindings[key] = value
    return bindings


def project_member_interfaces_from_graph(
    responsibility_edges: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Deterministically project frozen graph dependencies; no semantic selection."""
    return [
        {"interface_id": f"GRAPH_M2M_{index:04d}", "kind": "member_to_member",
         "source_member": source, "source_output": source_port,
         "target_member": target, "target_input": target_port,
         "goal": "project frozen workflow dependency"}
        for index, ((target, target_port), (source, source_port)) in enumerate(
            _member_dependency_bindings(responsibility_edges).items(), start=1
        )
    ]


def _compose_boundary_and_graph_interfaces(
    plan: dict[str, Any], responsibility_edges: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Discard model-authored internal edges and append the graph projection."""
    boundary = [value for value in plan.get("interfaces") or []
                if isinstance(value, dict) and value.get("kind") != "member_to_member"]
    return {"interfaces": [*boundary, *project_member_interfaces_from_graph(responsibility_edges)]}


def _semantic_source_capabilities(contract: dict[str, Any]) -> dict[str, Any]:
    """Describe generic selection affordances without choosing a mapping."""
    raw_type = str(contract.get("type") or "").strip()
    element_type: str | None = None
    if raw_type.lower().startswith("list[") and raw_type.endswith("]"):
        element_type = raw_type[5:-1].strip() or None
    elif raw_type.lower() in {"array", "list"}:
        items = contract.get("items")
        if isinstance(items, dict):
            element_type = _schema_type(items)
    return {
        "element_type": element_type,
        "supports_index_selection": element_type is not None,
    }


def _schema_type(schema: dict[str, Any]) -> str | None:
    """Return a declared type as descriptive planner context."""
    value = schema.get("type")
    return str(value).strip() if value is not None and str(value).strip() else None


def _semantic_identity(contract: dict[str, Any]) -> str:
    """Read a contract-declared semantic identity without consulting names."""
    for key in ("semantic_type", "semantic_value", "x-semantic-type"):
        value = contract.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def semantic_provenance_compatibility(
    *, source_role: str, source_schema: dict[str, Any], source_origin: str,
    target_role: str, target_schema: dict[str, Any],
) -> tuple[bool, str]:
    """Validate only the data types of two explicitly declared endpoints.

    Runtime provenance is deliberately outside Creator's contract.  Missing
    type annotations remain compatible for persisted contracts.
    """
    _ = source_role, source_origin, target_role
    source_type = (_schema_type(source_schema) or "").lower()
    target_type = (_schema_type(target_schema) or "").lower()
    aliases = {"list": "array", "dict": "object", "integer": "number", "float": "number"}
    source_type = aliases.get(source_type, source_type)
    target_type = aliases.get(target_type, target_type)
    if source_type and target_type and source_type != target_type:
        return False, f"source type {source_type!r} is incompatible with target type {target_type!r}"
    return True, "declared endpoint types are compatible"

def collect_interface_plan_validation_issues(
    *, plan: dict[str, Any], function_items: list[dict[str, Any]],
    platform_contract: dict[str, Any] | None = None,
    responsibility_edges: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Validate declared endpoint names and types, never runtime input sourcing."""
    compact = _compact_function_items(function_items)
    inputs = {item["target_file"]: {value["name"] for value in item["inputs"]} for item in compact}
    outputs = {item["target_file"]: {value["name"] for value in item["outputs"]} for item in compact}
    input_ports = {(item["target_file"], value["name"]): value for item in compact for value in item["inputs"]}
    output_ports = {(item["target_file"], value["name"]): value for item in compact for value in item["outputs"]}
    boundary = (platform_contract or {}).get("platform_skill_boundary", platform_contract or {})
    platform_inputs = {_compact_port_id(value) for value in boundary.get("input_envelope_fields") or []}
    platform_input_schemas = boundary.get("input_schemas") if isinstance(boundary.get("input_schemas"), dict) else {}
    platform_outputs = set(platform_output_names(platform_contract))
    required_platform_outputs = required_platform_output_fields(platform_contract)
    covered_platform: set[str] = set()
    issues: list[dict[str, Any]] = []

    def issue(code: str, path: str, interface_id: str, observed: Any, expected: Any, *, error_type: str = "binding_error") -> None:
        issues.append({"code": code, "stage": "interface_plan_validation", "path": path,
                       "error_type": error_type,
                       "interface_id": interface_id, "message": "Interface logical contract is invalid.",
                       "observed_value": observed, "expected_constraint": expected, "details": {}})

    invalid_required_outputs = required_platform_outputs - platform_outputs
    if invalid_required_outputs:
        issue("invalid_required_platform_output", "$.platform_contract.required_final_output_fields", "", sorted(invalid_required_outputs), sorted(platform_outputs))

    receiving_contracts: dict[tuple[str, str], list[str]] = {}
    graph_bindings = _member_dependency_bindings(responsibility_edges)

    for index, interface in enumerate(plan.get("interfaces") or []):
        path, iid, kind = f"$.interfaces[{index}]", str(interface.get("interface_id") or ""), interface.get("kind")
        if kind == "platform_to_member":
            source, target, slot = interface["source_platform_input"], interface["target_member"], interface["target_input"]
            receiving_contracts.setdefault((target, slot), []).append(iid)
            if (target, slot) in graph_bindings:
                issue("graph_dependency_source_replaced", path, iid,
                      {"kind": kind, "source": source},
                      {"kind": "member_to_member", "source": graph_bindings[(target, slot)]},
                      error_type="provenance_error")
            if source not in platform_inputs: issue("unknown_platform_logical_input", f"{path}.source_platform_input", iid, source, sorted(platform_inputs))
            if target not in inputs or slot not in inputs.get(target, set()): issue("unknown_interface_logical_input", f"{path}.target_input", iid, slot, sorted(inputs.get(target, set())))
            else:
                target_port = input_ports[(target, slot)]
                declared_root_schema = dict(platform_input_schemas.get(source) or {})
                source_schema = _resolve_declared_source_path(
                    declared_root_schema,
                    list(interface.get("source_path") or []),
                )
                # Persisted platform contracts may name a source without
                # carrying a schema.  In that case its type is unknown rather
                # than incompatible; runtime will interpret the raw value.
                compatible, reason = semantic_provenance_compatibility(
                    source_role="platform_input", source_schema=source_schema or {},
                    source_origin="platform_input", target_role=target_port["role"], target_schema=target_port["contract"],
                )
                if not compatible:
                    issue("incompatible_interface_types", path, iid, {"source_type": _schema_type(source_schema or {}), "target_type": _schema_type(target_port["contract"])}, reason, error_type="schema_error")
        elif kind == "member_to_member":
            source, output = interface["source_member"], interface["source_output"]
            target, slot = interface["target_member"], interface["target_input"]
            receiving_contracts.setdefault((target, slot), []).append(iid)
            expected_graph_source = graph_bindings.get((target, slot))
            if responsibility_edges is not None and expected_graph_source != (source, output):
                issue("member_binding_not_in_graph", path, iid, (source, output),
                      expected_graph_source, error_type="provenance_error")
            if source not in outputs or output not in outputs.get(source, set()): issue("unknown_interface_logical_output", f"{path}.source_output", iid, output, sorted(outputs.get(source, set())))
            if target not in inputs or slot not in inputs.get(target, set()): issue("unknown_interface_logical_input", f"{path}.target_input", iid, slot, sorted(inputs.get(target, set())))
            else:
                target_port = input_ports[(target, slot)]
                source_port = output_ports.get((source, output), {"role": "runtime_output", "contract": {}})
                compatible, reason = semantic_provenance_compatibility(
                    source_role=source_port["role"], source_schema=source_port["contract"],
                    source_origin="member_output", target_role=target_port["role"], target_schema=target_port["contract"],
                )
                if not compatible:
                    issue("incompatible_interface_types", path, iid, {"source_type": _schema_type(source_port["contract"]), "target_type": _schema_type(target_port["contract"])}, reason, error_type="schema_error")
            if source == target: issue("interface_self_connection", path, iid, source, "distinct members")
        elif kind == "member_to_platform":
            source, output, target = interface["source_member"], interface["source_output"], interface["target_platform_output"]
            if source not in outputs or output not in outputs.get(source, set()): issue("unknown_interface_logical_output", f"{path}.source_output", iid, output, sorted(outputs.get(source, set())))
            # Contract membership is deterministic. Semantic review is only
            # allowed to assess mappings whose two endpoints already exist.
            if target not in platform_outputs: issue("unknown_platform_logical_output", f"{path}.target_platform_output", iid, target, sorted(platform_outputs))
            else:
                covered_platform.add(target)
                sink = get_platform_output_sink(platform_contract, target) or {}
                source_port = output_ports.get((source, output), {"contract": {}})
                target_schema = dict(sink.get("value_schema") or {})
                compatible, reason = semantic_provenance_compatibility(
                    source_role="runtime_output", source_schema=source_port.get("contract") or {},
                    source_origin="member_output", target_role="platform_output", target_schema=target_schema,
                )
                if not compatible:
                    issue("incompatible_interface_types", path, iid,
                          {"source_type": _schema_type(source_port.get("contract") or {}),
                           "target_type": _schema_type(target_schema)},
                          reason, error_type="schema_error")
    for member, slot in sorted(set(input_ports) - set(receiving_contracts)):
        issue(
            "missing_interface_contract", "$.interfaces", "",
            {"target_member": member, "target_input": slot},
            "one source-to-target Interface", error_type="missing_source_error",
        )
    observed_member_bindings = {
        (value.get("target_member"), value.get("target_input")):
        (value.get("source_member"), value.get("source_output"))
        for value in plan.get("interfaces") or [] if value.get("kind") == "member_to_member"
    }
    for target, source in graph_bindings.items():
        if observed_member_bindings.get(target) != source:
            issue("missing_graph_dependency_interface", "$.interfaces", "",
                  {"target_member": target[0], "target_input": target[1]},
                  {"source_member": source[0], "source_output": source[1]},
                  error_type="missing_source_error")
    for (member, slot), interface_ids in sorted(receiving_contracts.items()):
        if len(interface_ids) > 1:
            issue(
                "multiple_runtime_sources", "$.interfaces", "",
                {"target_member": member, "target_input": slot, "interface_ids": interface_ids},
                "exactly one source-to-target Interface", error_type="provenance_error",
            )

    for output in sorted(required_platform_outputs - covered_platform):
        issue(
            "uncovered_required_platform_output",
            "$.interfaces",
            "",
            {
                "target_platform_output": output,
                "repair_constraint": {
                    "type": "missing_terminal_output_binding",
                    "target_platform_output": output,
                    "required_action": "establish_one_valid_member_to_platform_binding",
                },
            },
            "at least one Interface"
        )
    if not required_platform_outputs and not covered_platform:
        issue("missing_platform_terminal", "$.interfaces", "", {}, "at least one legal member_to_platform Interface")
    # Keep the established aggregate codes for callers while also exposing the
    # edge-addressable diagnostic consumed by patch repair.  An invalid edge is
    # never reported without its immutable interface identifier.
    by_id = {str(value.get("interface_id") or ""): value for value in plan.get("interfaces") or []}
    edge_issues: list[dict[str, Any]] = []
    for value in issues:
        iid = str(value.get("interface_id") or "")
        if not iid:
            continue
        interface = by_id.get(iid, {})
        if interface.get("kind") == "platform_to_member":
            source = {"platform_input": interface.get("source_platform_input"), "source_path": interface.get("source_path", [])}
        else:
            source = {"member": interface.get("source_member"), "output": interface.get("source_output")}
        target = ({"platform_output": interface.get("target_platform_output")}
                  if interface.get("kind") == "member_to_platform" else
                  {"member": interface.get("target_member"), "input": interface.get("target_input")})
        edge_issues.append({
            "code": "interface_binding_invalid", "stage": "interface_plan_validation",
            "interface_id": iid, "source": source, "target": target,
            "reason": value["code"], "expected": value.get("expected_constraint"),
            "error_type": value.get("error_type", "binding_error"), "path": value.get("path", "$.interfaces"),
            "message": "Interface binding is invalid.", "observed_value": value.get("observed_value"),
            "expected_constraint": value.get("expected_constraint"), "details": {"reason_code": value["code"]},
        })
    issues.extend(edge_issues)
    logger.info("[Creator][interface_contract_validation] runtime_input_validation_deferred=true input_contract_count=%d covered_input_contract_count=%d required_platform_output_count=%d covered_platform_output_count=%d",
                len(input_ports), len(set(input_ports) & set(receiving_contracts)),
                len(required_platform_outputs), len(required_platform_outputs & covered_platform))
    return issues


def interface_contract_closure_check(
    *, interface_plan: dict[str, Any], function_items: list[dict[str, Any]],
    platform_contract: dict[str, Any] | None = None,
    responsibility_edges: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Check that every frozen FunctionItem input has one planned interface."""
    return [
        issue for issue in collect_interface_plan_validation_issues(
            plan=interface_plan, function_items=function_items,
            platform_contract=platform_contract,
            responsibility_edges=responsibility_edges,
        )
        if issue.get("code") in {
            "missing_interface_contract", "missing_graph_dependency_interface",
            "graph_dependency_source_replaced", "member_binding_not_in_graph",
        }
    ]


def merge_interface_validation_issues(
    *issue_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Stably combine diagnostics without interpreting or rewriting them."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in issue_groups:
        for issue in group:
            key = json.dumps(issue, ensure_ascii=False, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            merged.append(issue)
    return merged


def _resolve_system_requirements_context(
    *, requirement_allocations: list[dict[str, Any]] | None,
    explicit_system_requirements: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Resolve system context once while preserving an explicit empty list."""
    if explicit_system_requirements is not None:
        return list(explicit_system_requirements)
    return [
        allocation for allocation in (requirement_allocations or [])
        if not (allocation.get("owners") or [])
    ]


def build_interface_repair_scope(
    validation_issues: list[dict[str, Any]],
    current_interface_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return stage authority without deriving a repair operation from issues."""
    _ = current_interface_plan
    policy = {
        "binding_error": {"reselect_legal_source"},
        "provenance_error": {"reselect_legal_source", "reclassify_input_role", "make_input_optional"},
        "missing_source_error": {"request_additional_input"},
        "schema_error": {"replace_source_or_target", "restore_declared_binding"},
    }
    error_types = {str(issue.get("error_type") or "binding_error") for issue in validation_issues}
    return {
        "editable_layer": "interface_plan",
        "frozen_layers": [
            "confirmed_requirements", "function_items", "requirement_channels",
        ],
        "preserve_unaffected_semantics": True,
        "max_semantic_repair_cycles": 2,
        "error_types": sorted(error_types),
        "failed_validation_reason": [
            {
                "error": issue.get("code"),
                **(issue.get("observed_value") if isinstance(issue.get("observed_value"), dict) else {}),
            }
            for issue in validation_issues
        ],
        "allowed_repairs": sorted(set().union(*(policy.get(value, set()) for value in error_types))),
        "forbidden_repairs": ["invent_source", "invent_transform", "infer_input_hierarchy"],
    }


def validate_interface_repair_scope(
    *, before: dict[str, Any], after: dict[str, Any], repair_scope: dict[str, Any]
) -> None:
    """Validate stage authority without selecting an Interface edit strategy."""
    before_list = before.get("interfaces") or []
    after_list = after.get("interfaces") or []
    before_by_id = {value.get("interface_id"): value for value in before_list}
    after_by_id = {value.get("interface_id"): value for value in after_list}
    if len(after_by_id) != len(after_list):
        _raise("interface_id must remain unique", "interface_repair_scope_error", path="$.interfaces")
    required_scope_fields = set(build_interface_repair_scope([]))
    if set(repair_scope) != required_scope_fields or repair_scope.get("editable_layer") != "interface_plan":
        _raise("invalid Interface repair stage authority", "interface_repair_scope_error", path="$.repair_scope")
    if before_list and not after_list:
        _raise("repair cannot erase the complete Interface Plan", "interface_repair_scope_error", path="$.interfaces")

def interface_receiving_slot_identity(interface: dict[str, Any]) -> tuple[str, str] | None:
    """
    Return the logical receiving slot satisfied by an Interface.

    Receiving slot identity is:
        (target_member, target_input)

    This is a contract identity, independent of:
    - business domain
    - input name
    - data type
    - implementation detail
    """
    kind = interface.get("kind")

    if kind in {
        "platform_to_member",
        "member_to_member",
    }:
        target_member = str(interface.get("target_member") or "").strip()
        target_input = str(interface.get("target_input") or "").strip()

        if target_member and target_input:
            return (
                target_member,
                target_input,
            )

    return None


def interface_receiving_slot_signature(
    plan: dict[str, Any]
) -> tuple[tuple[str, str], ...]:
    """
    Semantic signature of covered receiving slots.

    Used by refinement to distinguish:
    same-looking interface plans that still
    miss different contractual obligations.
    """
    slots = []

    for interface in plan.get("interfaces") or []:
        slot = interface_receiving_slot_identity(interface)
        if slot:
            slots.append(slot)

    return tuple(sorted(set(slots), key=repr))

def canonical_logical_binding_signatures(plan: dict[str, Any]) -> tuple[tuple[Any, ...], ...]:
    """Return a multiplicity-preserving semantic identity, ignoring presentation."""
    signatures = []
    for value in plan.get("interfaces") or []:
        kind = value.get("kind")
        if kind == "platform_to_member":
            signature = (kind, value.get("source_platform_input"), tuple(value.get("source_path") or []), value.get("target_member"), value.get("target_input"))
        elif kind == "member_to_member":
            signature = (kind, value.get("source_member"), value.get("source_output"), value.get("target_member"), value.get("target_input"))
        elif kind == "member_to_platform":
            signature = (kind, value.get("source_member"), value.get("source_output"), value.get("target_platform_output"))
        else:
            signature = (str(kind), json.dumps(value, sort_keys=True, default=str))
        signatures.append(signature)
    return tuple(sorted(signatures, key=repr))


def build_graph_obligations_from_interfaces(*, interface_plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert declared interface intents to local graph obligations."""
    obligations: list[dict[str, Any]] = []
    for interface in interface_plan.get("interfaces") or []:
        obligation = {
            "obligation_id": f"O{len(obligations) + 1:04d}",
            "interface_id": interface["interface_id"],
            "goal": interface.get("semantic_reason", "semantic source-target binding"),
        }
        if interface["kind"] == "platform_to_member":
            obligation.update({"kind": "platform_to_script", "source_platform_input": interface["source_platform_input"], "source_path": list(interface["source_path"]), "target_member": interface["target_member"], "target_input": interface["target_input"]})
        elif interface["kind"] == "member_to_member":
            obligation.update({"kind": "script_to_script", "source_member": interface["source_member"], "source_output": interface["source_output"], "target_member": interface["target_member"], "target_input": interface["target_input"]})
        else:
            obligation.update({"kind": "script_to_platform", "source_member": interface["source_member"], "source_output": interface["source_output"], "target_platform_output": interface["target_platform_output"]})
        obligations.append(obligation)
    return obligations


def _interface_plan_prompt() -> str:
    return f"""{AUTHORITY_CONTRACT}

    {INTERFACE_CONTRACT_SCOPE_CONTRACT}

    {CANONICAL_INTERFACE_CONTRACT_RULE}

    {PLATFORM_OUTPUT_CONTRACT}

    {PLATFORM_OUTPUT_MAPPING_CONTRACT}

    {PLATFORM_BOUNDARY_CONTRACT}

    {RUNTIME_INPUT_PROVENANCE_CONTRACT}

1. AUTHORITATIVE FACTS
The payload contains confirmed requirements, frozen FunctionItems and their
logical input/output contracts. Logical port contracts include type constraints.
When creating Interface bindings, preserve the declared type and cardinality
of the logical ports. Do not change a single-value logical input into a
different structural form unless the FunctionItem contract explicitly declares
that structure. Runtime user-parameter provenance is not part of this payload.

Logical ports are declared FunctionItem/platform input or output names. Opaque
endpoint IDs are later registry identities such as INxxxx/OUTxxxx/PINxxxx/POUTxxxx.

2. SHARED CONTRACTS
WIRE CONTRACT
platform_to_member always contains: interface_id, kind,
source_type="platform_input", source_platform_input, source_path,
target_member, target_input.
member_to_member always contains: interface_id, kind, source_member,
source_output, target_member, target_input.
member_to_platform always contains: interface_id, kind, source_member,
source_output, target_platform_output.
Every kind may contain semantic_reason explaining why the source satisfies the
target. It is optional and never changes the binding.
Interface planning only determines semantic connections. The planner MUST NOT
generate conversion operations or invent transform names. Representation
conversion is handled by the runtime capability layer.
This wire contract and INTERFACE_SCHEMA describe the same protocol. Do not omit
a required field because its value is empty-like; source_path=[] is the explicit
representation of whole-slot platform binding.

3. CURRENT TASK
Produce the platform-boundary portion of the Interface Plan for unlocked
FunctionItem inputs and required final outputs. Bind invocation parameters
from declared platform inputs. Optionality and defaults may defer runtime
value validation, but never defer the source-to-target contract.
Return only platform_to_member and member_to_platform records. Inputs listed
in locked_member_interfaces already have frozen graph provenance: do not emit
or replace their member_to_member records.

4. CURRENT AUTHORITY
You, not the backend, own and choose the semantic producer using responsibilities, port
descriptions and contracts, confirmed requirements, workflow semantics, and the
platform contract. Do not choose by field-name similarity alone. Never choose
from target-port name similarity.

INTERFACE BINDING AUTHORITY
Structured logical binding fields are authoritative for transfer identity.
semantic_reason optionally explains why the declared logical source satisfies the declared receiving slot;
it does not redefine, broaden, merge, or replace that binding. One Interface is
one declared logical source -> one declared receiving slot -> one future edge.
Two independently selectable receiving slots require separate records. A source
output may be reused by separate Interfaces.

5. HARD ACCEPTANCE CONDITIONS
FUNCTIONITEM CONTRACT BEFORE RUNTIME BINDING
Every FunctionItem input must have exactly one Interface during creation. Keep
structured business values such as options as one object port; never expand
options.font or other object properties into new logical inputs. Runtime only
validates whether an optional/defaulted value is present.

Do not modify FunctionItems, requirements, channels, or logical ports. Do not
return opaque endpoint IDs, Graph edges, or extra fields. Do not
invent platform inputs to close coverage.

6. SILENT SELF-CHECK
For each receiving slot, first identify where the required semantic value
actually exists.

If it exists in the external platform input contract, choose
platform_to_member.

If it is produced by another frozen FunctionItem, choose member_to_member.

Use member_to_platform only for a semantic value that leaves the Skill
through the platform output boundary.

Do not bind merely because a platform source exists. Verify only that each
declared transfer references real contract ports with compatible types and that
each required final platform output has a producer.

OUTPUT FIELD IDENTITY CHECK

Before returning:
For every member_to_platform Interface:

1. Identify the semantic value produced by the FunctionItem.
2. Identify the existing platform output field that represents the required
external result.
3. Verify target_platform_output is copied from the platform contract exactly.

Do not output a descriptive label as target_platform_output.
Do not output a format name, file format name, or presentation name unless it
is explicitly declared as a platform output field.

Never output transform, adapter, serializer, or conversion fields.

7. OUTPUT CONTRACT
Return only strict JSON matching this schema:
{json.dumps(INTERFACE_SCHEMA, ensure_ascii=False)}
""".strip()


async def _reformat_interface_plan_response(*, raw_response: str, validation_error: InterfaceIntentPlanError, planner_model: str, model_call: ModelCall) -> dict[str, Any]:
    logger.info("[Creator][interface_protocol_repair] attempt=1 error_paths=%s", [validation_error.details.get("path", "$")])
    prompt = """PROTOCOL REPAIR AUTHORITY
Your only task is to restore parseable JSON transport while preserving all
semantic content already present in the raw response.
Allowed: repair JSON syntax; remove accidental Markdown fencing when needed;
restore valid JSON object/array syntax; repair quoting, commas, brackets, and
equivalent serialization defects.
Forbidden: create a missing Interface; create a missing logical binding field;
choose a source or target; invent source_path; infer source_output or
target_input; change Interface direction; invent or replace business-semantic
values.
If the response becomes parseable JSON but does not satisfy the Interface
contract, stop there. Semantic correction belongs to the Interface Planner.
Return strict parseable JSON only."""
    payload = {"schema": INTERFACE_SCHEMA, "raw_response": raw_response, "validation_error": {"code": validation_error.code, "details": validation_error.details, "message": str(validation_error)}}
    text = await model_call([{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}], planner_model)
    return _parse_object(text)


INTERFACE_REVIEW_ISSUE_FIELDS = {
    "error_type",
    "message",
    "affected_interfaces",
    "affected_inputs",
    "affected_outputs",
    "evidence"
}
INTERFACE_REVIEW_SCHEMA = {
    "passed": "boolean",
    "issues": [
        {
            "error_type": "binding_error | provenance_error | missing_source_error | schema_error",
            "message": "string",
            "affected_interfaces": ["string"],
            "affected_inputs": [
                {
                    "target_member": "string",
                    "target_input": "string"
                }
            ],
            "affected_outputs": [
                {
                    "source_member": "string",
                    "source_output": "string",
                    "target_platform_output": "string"
                }
            ],
            "evidence": {
                "observed": "any",
                "expected": "any"
            }
        }
    ]
}

def normalize_interface_review_issue(raw_issue: dict[str, Any], frozen_function_items: list[dict[str, Any]], current_interface_plan: dict[str, Any], *, path: str = "$.issues[]") -> dict[str, Any]:
    """Validate a free-form semantic defect envelope and logical references."""
    if not isinstance(raw_issue, dict) or set(raw_issue) != INTERFACE_REVIEW_ISSUE_FIELDS:
        _raise("semantic review issue has invalid shape", "invalid_interface_semantic_review_protocol", path=path)
    error_type = str(raw_issue["error_type"] or "").strip()
    if error_type not in REVIEW_ERROR_TYPES:
        _raise("semantic review issue has invalid error_type", "invalid_interface_semantic_review_protocol", path=f"{path}.error_type")
    message = raw_issue["message"]
    interface_ids = raw_issue["affected_interfaces"]
    affected_inputs = raw_issue["affected_inputs"]
    affected_outputs = raw_issue["affected_outputs"]
    evidence = raw_issue["evidence"]
    if (
            not isinstance(message, str)
            or not message.strip()
            or not isinstance(interface_ids, list)
            or not isinstance(affected_inputs, list)
            or not isinstance(affected_outputs, list)
            or not isinstance(evidence, dict)
    ):
        _raise("semantic review issue is not auditable", "invalid_interface_semantic_review_protocol", path=path)
    known_interfaces = {str(value.get("interface_id") or "") for value in current_interface_plan.get("interfaces") or []}
    inputs = {item["target_file"]: {value["name"] for value in item["inputs"]} for item in _compact_function_items(frozen_function_items)}
    if any(not isinstance(value, str) or value not in known_interfaces for value in interface_ids):
        _raise("review issue references an unknown Interface", "invalid_interface_semantic_review_reference", path=f"{path}.affected_interfaces")
    normalized_inputs = []
    normalized_outputs = []

    for index, value in enumerate(affected_outputs):
        if not isinstance(value, dict):
            _raise(
                "review issue affected_outputs must be object",
                "invalid_interface_semantic_review_reference",
                path=f"{path}.affected_outputs[{index}]"
            )

        required = {
            "source_member",
            "source_output",
            "target_platform_output",
        }

        if set(value) != required:
            _raise(
                "review issue references invalid affected output shape",
                "invalid_interface_semantic_review_reference",
                path=f"{path}.affected_outputs[{index}]"
            )

        normalized_outputs.append(dict(value))
    for index, value in enumerate(affected_inputs):
        if not isinstance(value, dict) or set(value) != {"target_member", "target_input"} or value.get("target_input") not in inputs.get(value.get("target_member"), set()):
            _raise("review issue references an unknown logical input", "invalid_interface_semantic_review_reference", path=f"{path}.affected_inputs[{index}]")
        normalized_inputs.append(dict(value))
    envelope = {
        "error_type": error_type,
        "message": message.strip(),
        "affected_interfaces": list(interface_ids),
        "affected_inputs": normalized_inputs,
        "affected_outputs": normalized_outputs,
        "evidence": dict(evidence)
    }
    return {**envelope, "stage": "interface_semantic_review", "path": path, "interface_id": interface_ids[0] if interface_ids else "", "details": envelope}


CRITIC_SCHEMA = {"diagnosis": "string", "required_postcondition": "string"}


def validate_interface_repair_critic(value: dict[str, Any], *, validation_issues: list[dict[str, Any]], current_interface_plan: dict[str, Any], frozen_function_items: list[dict[str, Any]], repair_scope: dict[str, Any]) -> dict[str, Any]:
    """Validate Critic transport only; revalidation proves semantic repair."""
    _ = validation_issues, current_interface_plan, frozen_function_items, repair_scope
    if not isinstance(value, dict) or set(value) != {"diagnosis", "required_postcondition"}:
        _raise("repair critic response has invalid fields", "invalid_interface_repair_critic_protocol", path="$")
    for field in ("diagnosis", "required_postcondition"):
        if not isinstance(value[field], str) or not value[field].strip():
            _raise(f"{field} must be non-empty", "invalid_interface_repair_critic_protocol", path=f"$.{field}")
    return {field: value[field].strip() for field in ("diagnosis", "required_postcondition")}


def _validate_interface_review_response(
    *, value: dict[str, Any], interface_plan: dict[str, Any],
    frozen_function_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if (set(value) != {"passed", "issues"} or not isinstance(value.get("passed"), bool)
            or not isinstance(value.get("issues"), list)):
        _raise("semantic review response has invalid shape", "invalid_interface_semantic_review_protocol", path="$")
    issues = [
        normalize_interface_review_issue(raw, frozen_function_items, interface_plan, path=f"$.issues[{index}]")
        for index, raw in enumerate(value["issues"])
    ]
    if value["passed"] != (not issues):
        _raise("semantic review passed flag contradicts issues", "invalid_interface_semantic_review_protocol", path="$.passed")
    return issues


async def _reformat_interface_review_response(
    *, raw_response: str, validation_error: InterfaceIntentPlanError,
    reviewer_model: str, model_call: ModelCall,
) -> dict[str, Any]:
    prompt = """Repair only the JSON protocol shape.
Preserve every semantic conclusion, message, affected reference, and evidence.
Do not add, remove, merge, split, or reinterpret issues.
Every issue must use the single supplied taxonomy-free issue schema.
Return only the corrected JSON object."""
    payload = {
        "review_schema": INTERFACE_REVIEW_SCHEMA,
        "raw_response": raw_response,
        "validation_error": {
            "code": validation_error.code, "message": str(validation_error),
            "details": validation_error.details,
        },
    }
    logger.info("[Creator][interface_semantic_review_protocol_repair] attempt=1")
    text = await model_call(
        [{"role": "system", "content": prompt},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        reviewer_model,
    )
    return _parse_object(text)


async def review_interface_plan_semantically(
    *, original_user_goal: str, frozen_function_items: list[dict[str, Any]],
    interface_plan: dict[str, Any], requirement_allocations: list[dict[str, Any]] | None,
    requirement_channels: dict[str, str] | None,
    system_requirements: list[dict[str, Any]] | None,
    platform_contract: dict[str, Any] | None, reviewer_model: str,
    model_call: ModelCall, review_mode: str = "full",
) -> list[dict[str, Any]]:
    """Ask once for semantic diagnostics; never ask the reviewer for a repair."""
    if review_mode not in {"full", "existing_bindings_only"}:
        raise ValueError(f"unsupported Interface semantic review mode: {review_mode}")
    mode_contract = "" if review_mode == "full" else """

EXISTING-BINDING-ONLY MODE
Coverage may currently be incomplete.

Do NOT report:
- missing receiving slots
- missing Interfaces
- duplicate provenance
- missing platform terminal
- graph completeness

Those are deterministic backend responsibilities. Review ONLY semantic
correctness of Interfaces that already exist, including platform_to_member,
member_to_member, and member_to_platform records.

STRUCTURED BINDING AUTHORITY
The semantic transfer is defined by structured binding fields. goal is
explanatory only. If goal describes one semantic value while the structured
source actually declares another value, judge the structured binding itself.
A plausible goal cannot make an incorrect structured source/target binding valid.
"""
    prompt = AUTHORITY_CONTRACT + """

    """ + CANONICAL_INTERFACE_CONTRACT_RULE + """

    """ + PLATFORM_OUTPUT_CONTRACT + """

    """ + PLATFORM_OUTPUT_MAPPING_CONTRACT + """

    """ + PLATFORM_BOUNDARY_CONTRACT + """

    """ + PLATFORM_INPUT_HIERARCHY_CONTRACT + """

    """ + RUNTIME_INPUT_PROVENANCE_CONTRACT + """

    """ + MULTIMODAL_INPUT_PROVENANCE_CONTRACT + """

    """ + SOURCE_PATH_CONTRACT + """

    1. AUTHORITATIVE FACTS

    The payload contains:

    - confirmed user requirements
    - frozen FunctionItems
    - frozen logical input/output contracts
    - runtime source facts
    - immutable platform contract
    - complete Interface Plan

    These facts are authoritative.

    The Interface Plan declares semantic bindings between logical ports.

    The reviewer does not redesign the system and does not replace upstream contracts.


    2. DETERMINISTIC VALIDITY PRECONDITION

    The backend has already validated:

    - Interface schema
    - Interface kind correctness
    - logical member references
    - logical input existence
    - logical output existence
    - platform output identifier existence
    - required structural coverage

    Do NOT report deterministic validation failures as semantic defects.


    3. PLATFORM OUTPUT IMMUTABILITY RULE

    Platform output fields are external contract identifiers.

    final_output_fields is the authoritative legal output domain.

    The reviewer MUST NOT:

    - rename platform output fields
    - replace platform output fields
    - prefer another output name
    - judge whether an output field name is intuitive
    - infer a better output field

    Example:

    Platform contract:

    final_output_fields:
    [
        "file_outputs",
        "text"
    ]


    Interface:

    source_output:
    file_outputs

    target_platform_output:
    file_outputs


    This is a valid binding if the semantic value matches.

    The reviewer MUST NOT suggest:

    file_outputs -> file_paths

    unless the platform contract explicitly defines file_paths as the target contract field.


    4. SEMANTIC REVIEW TASK

    Only review semantic compatibility.

    For every Interface:

    Check:

    A. Does the declared source produce the semantic value required by the target?

    B. Does the receiving logical port accept that semantic value?

    C. Does the Interface kind match the actual provenance?

    D. Does source_path select the intended semantic value when used?


    Do NOT check:

    - whether a source or target field exists (deterministic validation owns this)
    - whether source and target schemas are structurally equal
    - whether a field name looks natural
    - whether another field name would be clearer
    - whether another design would be preferred
    - whether a valid contract field should be renamed


    5. OUTPUT BOUNDARY REVIEW

    For member_to_platform:

    Evaluate:

    source_member.source_output
            ->
    target_platform_output

    Do not report a mapping as a type mismatch merely because its endpoint
    schemas differ. Representation adaptation is resolved only at runtime.


    The reviewer checks semantic compatibility only.

    The reviewer does NOT reinterpret:

    source_output names.

    The reviewer does NOT create new platform outputs.

    The reviewer does NOT replace declared platform outputs.


    6. ISSUE EVIDENCE STANDARD

    Only report a defect when:

    - the Interface is structurally valid;
    - the referenced ports exist;
    - a concrete semantic mismatch exists.

    A different possible design is not a defect.

    A preferred naming style is not a defect.

    A valid alternative mapping is not a defect.


    7. AUTHORITY LIMIT

    The reviewer MUST NOT:

    - modify Interface records;
    - generate repaired plans;
    - propose edits;
    - select different sources;
    - select different targets;
    - create Graph edges;
    - use opaque endpoint IDs.

    The reviewer only returns semantic diagnostics.


    8. OUTPUT CONTRACT

    Classify every defect by its primary failed dimension:
    binding_error (boundary/reference), provenance_error (semantic origin),
    missing_source_error (required source absent), or schema_error (contract structure).
    Independently check schema correctness, boundary correctness, and
    provenance correctness. Do not collapse these into "interface invalid".

    passed=true exactly when issues is empty.

    Return only strict JSON:

    """ + json.dumps(INTERFACE_REVIEW_SCHEMA, ensure_ascii=False)
    payload = {
        "system_goal": original_user_goal,
        "function_items": _compact_function_items(frozen_function_items),
        "current_interface_plan": interface_plan,
        "requirement_allocations": requirement_allocations or [],
        "requirement_channels": requirement_channels or {},
        "unowned_system_requirements": system_requirements or [],
        "platform_contract": platform_contract or {},
    }
    raw_response = await model_call(
            [{"role": "system", "content": prompt},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
            reviewer_model,
        )
    try:
        issues = _validate_interface_review_response(
            value=_parse_object(raw_response), interface_plan=interface_plan,
            frozen_function_items=frozen_function_items,
        )
    except InterfaceIntentPlanError as original_exc:
        try:
            reformatted = await _reformat_interface_review_response(
                raw_response=raw_response, validation_error=original_exc,
                reviewer_model=reviewer_model, model_call=model_call,
            )
            issues = _validate_interface_review_response(
                value=reformatted, interface_plan=interface_plan,
                frozen_function_items=frozen_function_items,
            )
        except InterfaceIntentPlanError as repair_exc:
            logger.info("[Creator][interface_semantic_review] result=failed error_code=%s", repair_exc.code)
            raise InterfaceIntentPlanError(
                "interface semantic review failed", code="interface_semantic_review_failed",
                details={
                    "review_attempts": 1, "protocol_repair_attempts": 1,
                    "original_error": {"code": original_exc.code, "message": str(original_exc), "details": original_exc.details},
                    "repair_error": {"code": repair_exc.code, "message": str(repair_exc), "details": repair_exc.details},
                },
            ) from repair_exc
        logger.info("[Creator][interface_semantic_review_protocol_repair] attempt=1 result=success")
    except Exception:
        # Model transport failures remain transport failures, not protocol repair.
        raise
    logger.info("[Creator][interface_semantic_review] result=%s issue_count=%d", "passed" if not issues else "issues_found", len(issues))
    return issues


async def plan_function_item_interfaces(*, original_user_goal: str, frozen_function_items: list[dict[str, Any]], requirement_allocations: list[dict[str, Any]] | None = None, requirement_channels: dict[str, str] | None = None, system_requirements: list[dict[str, Any]] | None = None, interaction_requirements: list[dict[str, Any]] | None = None, platform_contract: dict[str, Any] | None = None, responsibility_edges: list[dict[str, Any]] | None = None, skill_name: str = "", planner_model: str, model_call: ModelCall, reviewer_model: str | None = None, reviewer_model_call: ModelCall | None = None) -> dict[str, Any]:
    """Ask the model for interaction intents between frozen FunctionItems."""

    system_requirements_context = _resolve_system_requirements_context(
        requirement_allocations=requirement_allocations,
        explicit_system_requirements=(
            system_requirements if system_requirements is not None
            else interaction_requirements
        ),
    )
    payload = {
        "system_goal": original_user_goal,
        "skill_name": skill_name,
        "function_items": _compact_function_items(frozen_function_items),
        "executable_requirement_allocations": [
            allocation
            for allocation in (requirement_allocations or [])
            if requirement_channels and requirement_channels.get(str(allocation.get("requirement_id") or "")) == "executable"
        ],
        "requirement_channels": requirement_channels or {},
        "unowned_system_requirements": system_requirements_context,
        "platform_contract": platform_contract or {},
        "frozen_interface_slots": build_frozen_interface_slots(
            function_items=frozen_function_items, platform_contract=platform_contract,
        ),
        "semantic_mapping_candidates": build_semantic_mapping_candidates(
            function_items=frozen_function_items, platform_contract=platform_contract,
            responsibility_edges=responsibility_edges,
        ),
        "locked_member_interfaces": project_member_interfaces_from_graph(responsibility_edges),
    }
    raw_response = await model_call([{"role": "system", "content": _interface_plan_prompt()}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}], planner_model)
    try:
        transport = _parse_object(raw_response)
    except InterfaceIntentPlanError as exc:
        # Routing is by the failed parse stage, never by a semantic error code.
        transport = await _reformat_interface_plan_response(
            raw_response=raw_response, validation_error=exc,
            planner_model=planner_model, model_call=model_call,
        )
        logger.info("[Creator][interface_protocol_repair] attempt=1 result=transport_parseable")
    protocol_issue: InterfaceIntentPlanError | None = None
    try:
        parsed = validate_interface_plan_protocol(transport)
        if responsibility_edges is not None:
            parsed = validate_interface_plan_protocol(
                _compose_boundary_and_graph_interfaces(parsed, responsibility_edges)
            )
    except InterfaceIntentPlanError as exc:
        protocol_issue = exc
        parsed = transport
    deterministic_issues = collect_interface_plan_validation_issues(
        plan=parsed, function_items=frozen_function_items, platform_contract=platform_contract,
        responsibility_edges=responsibility_edges,
    ) if protocol_issue is None else []
    logger.info(
        "[Creator][interface_validation] stage=deterministic initial_issue_count=%d",
        len(deterministic_issues),
    )
    if protocol_issue is not None or deterministic_issues:
        facts = ([{"code": protocol_issue.code, "message": str(protocol_issue), "details": protocol_issue.details}]
                 if protocol_issue is not None else deterministic_issues)
        correction_prompt = f"""{AUTHORITY_CONTRACT}

        {INTERFACE_CONTRACT_SCOPE_CONTRACT}

        {VALID_INTERFACE_FREEZE_CONTRACT}

        {PLATFORM_OUTPUT_CONTRACT}

        {PLATFORM_OUTPUT_MAPPING_CONTRACT}

        {PLATFORM_BOUNDARY_CONTRACT}

        {RUNTIME_INPUT_PROVENANCE_CONTRACT}

INTERFACE PLAN CORRECTION
1. AUTHORITATIVE FACTS
Confirmed requirements, frozen FunctionItems, logical ports, platform contract,
and shared Interface contracts remain authoritative. Runtime user-parameter
sources are intentionally unavailable to Creator.
2. SHARED CONTRACTS
INTERFACE_SCHEMA and the shared contracts above define the protocol.
3. CURRENT TASK
The previous plan failed deterministic acceptance. When its protocol is valid,
return only targeted patch operations and resolve every supplied acceptance
failure simultaneously. A protocol-invalid transport may be reconstructed only
to restore the wire shape. Facts describe invalid state.

Do not patch only visible wording. Never regenerate all interfaces for a
semantically valid transport.
4. CURRENT AUTHORITY
Modify only Interfaces explicitly named by violations. Use only
remove_interface, replace_source, or replace_target. Never
add an Interface and never regenerate or reorder the complete contract.
5. HARD ACCEPTANCE CONDITIONS
The complete result must match INTERFACE_SCHEMA. Every supplied acceptance fact
is independently blocking; coverage alone is insufficient.
6. SILENT SELF-CHECK
Silently verify that every interface present uses declared source and target
parameter names and compatible data types. Do not repair an absent input by
adding or changing a platform mapping; that repair belongs solely to the
FunctionItem input contract.
7. OUTPUT CONTRACT
For deterministic semantic correction return strict JSON matching
INTERFACE_PATCH_SCHEMA only. For protocol-shape correction only, return strict
JSON matching INTERFACE_SCHEMA."""
        correction_prompt = f"{correction_prompt}\n\n{REFINEMENT_FEEDBACK_CONTRACT}"

        async def propose_correction(previous_candidate: Any, feedback: dict[str, Any]) -> Any:
            correction_payload = {
                **payload,
                "refinement_feedback": {
                    **feedback,
                    "issue_code_set_before": sorted({str(issue.get("code") or "") for issue in facts if issue.get("code")}),
                    "issue_code_set_after": sorted({str(issue.get("code") or "") for issue in feedback.get("acceptance_facts", []) if issue.get("code")}),
                },
                "interface_schema": INTERFACE_SCHEMA,
                "interface_patch_schema": INTERFACE_PATCH_SCHEMA,
                "interfaces": (previous_candidate.get("interfaces") or []) if isinstance(previous_candidate, dict) else [],
                "violations": facts,
                "allowed_operations": ["remove_interface", "replace_source", "replace_target"],
            }
            if _include_previous_interface_plan(feedback):
                correction_payload["previous_interface_plan"] = previous_candidate
            corrected_text = await model_call(
                [{"role": "system", "content": correction_prompt},
                 {"role": "user", "content": json.dumps(correction_payload, ensure_ascii=False, default=str)}],
                planner_model,
            )
            try:
                corrected = _parse_object(corrected_text)
                if protocol_issue is None:
                    return validate_interface_patch(
                        plan=previous_candidate, patch=corrected,
                        function_items=frozen_function_items,
                        platform_contract=platform_contract, violations=facts,
                    )
                return corrected
            except InterfaceIntentPlanError:
                return {"__invalid_transport__": corrected_text}

        async def evaluate_correction(candidate_object: Any) -> CandidateEvaluation:
            try:
                candidate = validate_interface_plan_protocol(candidate_object)
                if responsibility_edges is not None:
                    candidate = validate_interface_plan_protocol(
                        _compose_boundary_and_graph_interfaces(candidate, responsibility_edges)
                    )
            except InterfaceIntentPlanError as exc:
                return CandidateEvaluation(
                    accepted=False, candidate=candidate_object,
                    acceptance_facts=[{"code": exc.code, "message": str(exc), "details": exc.details}],
                    semantic_comparable=False,
                )
            remaining = collect_interface_plan_validation_issues(
                plan=candidate, function_items=frozen_function_items,
                platform_contract=platform_contract,
                responsibility_edges=responsibility_edges,
            )
            return CandidateEvaluation(
                accepted=not remaining, candidate=candidate,
                acceptance_facts=remaining, semantic_comparable=True,
            )

        initial_evaluation = CandidateEvaluation(
            accepted=False, candidate=parsed, acceptance_facts=facts,
            semantic_comparable=protocol_issue is None,
        )
        try:
            parsed = await bounded_refine_candidate(
                stage="interface_plan_correction", initial_candidate=parsed,
                initial_evaluation=initial_evaluation, propose=propose_correction,
                evaluate=evaluate_correction,
                semantic_signature=lambda value: (
                    canonical_logical_binding_signatures(value),
                    interface_receiving_slot_signature(value),
                )
                if isinstance(value, dict) and "interfaces" in value else value,
                max_attempts=2,
            )
            deterministic_issues = []
        except BoundedRefinementFailed as exc:
            raise InterfaceIntentPlanError(
                "interface plan deterministic closure failed after correction",
                code="interface_plan_deterministic_closure_failed",
                details={"correction_attempts": exc.attempt,
                         "semantic_changed": exc.semantic_changed,
                         "remaining_issues": exc.evaluation.acceptance_facts},
            ) from exc
    review_issues: list[dict[str, Any]] = []
    if reviewer_model:
        review_plan = (_compose_boundary_and_graph_interfaces(parsed, [])
                       if responsibility_edges is not None else parsed)
        review_issues = await review_interface_plan_semantically(
            original_user_goal=original_user_goal,
            frozen_function_items=frozen_function_items,
            interface_plan=review_plan,
            requirement_allocations=requirement_allocations,
            requirement_channels=requirement_channels,
            system_requirements=system_requirements_context,
            platform_contract=platform_contract,
            reviewer_model=reviewer_model,
            model_call=reviewer_model_call or model_call,
        )
    combined_issues = merge_interface_validation_issues(review_issues)
    logger.info(
        "[Creator][interface_validation] stage=initial deterministic_issue_count=%d review_issue_count=%d combined_issue_count=%d repairable=%s",
        len(deterministic_issues), len(review_issues), len(combined_issues), bool(combined_issues),
    )
    if combined_issues:
        repair_plan = (_compose_boundary_and_graph_interfaces(parsed, [])
                       if responsibility_edges is not None else parsed)
        repaired = await repair_interface_plan_semantically(
            original_user_goal=original_user_goal,
            frozen_function_items=frozen_function_items,
            current_interface_plan=repair_plan,
            validation_issues=combined_issues,
            repair_scope=build_interface_repair_scope(combined_issues, parsed),
            requirement_allocations=requirement_allocations,
            requirement_channels=requirement_channels,
            system_requirements=system_requirements_context,
            platform_contract=platform_contract,
            skill_name=skill_name,
            planner_model=planner_model,
            model_call=model_call,
            reviewer_model=reviewer_model,
            reviewer_model_call=reviewer_model_call,
        )
        if responsibility_edges is not None:
            repaired = _compose_boundary_and_graph_interfaces(repaired, responsibility_edges)
        return validate_interface_intent_plan(
            plan=repaired, function_items=frozen_function_items,
        )
    return validate_interface_intent_plan(plan=parsed, function_items=frozen_function_items)


async def repair_interface_plan_semantically(
    *, original_user_goal: str, frozen_function_items: list[dict[str, Any]],
    current_interface_plan: dict[str, Any], validation_issues: list[dict[str, Any]],
    repair_scope: dict[str, Any], requirement_allocations: list[dict[str, Any]] | None = None,
    requirement_channels: dict[str, str] | None = None,
    system_requirements: list[dict[str, Any]] | None = None,
    interaction_requirements: list[dict[str, Any]] | None = None,
    platform_contract: dict[str, Any] | None = None, skill_name: str = "",
    repair_stage: str = "initial_interface_validation",
    planner_model: str, model_call: ModelCall, reviewer_model: str | None = None,
    reviewer_model_call: ModelCall | None = None,
) -> dict[str, Any]:
    """Perform one Critic call and at most two Generator attempts."""
    system_requirements_context = (
        list(system_requirements) if system_requirements is not None
        else _resolve_system_requirements_context(
            requirement_allocations=requirement_allocations,
            explicit_system_requirements=interaction_requirements,
        )
    )
    logger.info(
        "[Creator][interface_semantic_repair] stage=%s attempt=1 issue_count=%d editable_layer=%s max_cycles=%d",
        repair_stage, len(validation_issues), repair_scope.get("editable_layer", ""),
        int(repair_scope.get("max_semantic_repair_cycles") or 0),
    )

    critic_prompt = f"""{AUTHORITY_CONTRACT}

{PLATFORM_OUTPUT_CONTRACT}

{RUNTIME_INPUT_PROVENANCE_CONTRACT}

{PLATFORM_OUTPUT_MAPPING_CONTRACT}

{SOURCE_PATH_CONTRACT}

{PLATFORM_INPUT_HIERARCHY_CONTRACT}

{MULTIMODAL_INPUT_PROVENANCE_CONTRACT}

{RUNTIME_REPAIR_RESTRICTION_CONTRACT}

1. AUTHORITATIVE FACTS
The payload contains the failed complete Interface Plan; frozen FunctionItems
and required/default input facts; the platform contract; requirements; unified
blocking issues and deterministic Graph feedback; legal member, input, and
Interface ID domains; and repair_scope. Blocking facts identify what is invalid
or missing. They do not prescribe the business-semantic source.

2. DIAGNOSIS TASK
Diagnose the semantic root cause of all supplied failures as one coherent system
problem, then state what must become true in the repaired Interface Plan.
Validation facts identify deterministic or semantic contract failures; they do
not prescribe repair. Error codes describe observed failures. They are not repair
instructions. Do not translate error codes into mechanical edit actions.

GRAPH FEEDBACK AUTHORITY
Graph feedback reports deterministic observations only. A graph failure does
not itself explain the semantic cause. Do not treat an error code as a repair
instruction.

3. REQUIRED POSTCONDITION CONTRACT
required_postcondition is a declarative condition that must be true after repair, not an edit operation.
4. AUTHORITY LIMIT
Do not output a repaired plan, modify frozen FunctionItems, or prescribe adding/removing/splitting/merging an Interface, an Interface ID, a patch, an edit sequence, or endpoint IDs. The Generator owns all repair operations.
5. OUTPUT CONTRACT
Return only {{"diagnosis":"...","required_postcondition":"..."}}."""

    prompt = f"""{AUTHORITY_CONTRACT}

{PLATFORM_OUTPUT_CONTRACT}

{RUNTIME_INPUT_PROVENANCE_CONTRACT}

{SOURCE_PATH_CONTRACT}

{PLATFORM_INPUT_HIERARCHY_CONTRACT}

{PLATFORM_OUTPUT_MAPPING_CONTRACT}

{MULTIMODAL_INPUT_PROVENANCE_CONTRACT}

{RUNTIME_REPAIR_RESTRICTION_CONTRACT}

1. AUTHORITATIVE FACTS
The payload contains the complete failed Interface Plan, frozen FunctionItems,
requirements and platform contract, blocking issues, deterministic Graph
feedback, one validated Critic diagnosis, legal Interface schema,
legal member/input domains, and repair_scope.

2. TASK
Repair the Interface Plan so all supplied blocking facts are resolved simultaneously.
For a platform-output validation failure, use failed_validation_reason to repair
only the incompatible source or target contract tuple. Do not guess
a replacement output merely from its field name and do not redesign the whole
Interface Plan.

3. SEMANTIC RESPONSIBILITY
Deterministic repair constraints are contractual obligations.

When validation_issues contain repair_constraint:

- treat the constraint as a required postcondition;
- do not reinterpret the obligation;
- determine only the missing semantic provenance.

When validation_issues contain provenance_candidates:

- treat them as legal provenance search results;
- select a candidate only when it satisfies the FunctionItem contract;
- if none is semantically valid, reconstruct from authoritative upstream facts;
- do not solve missing coverage by renaming ports or inventing new platform inputs.
The backend defines what contract obligation is missing.
The Generator decides how the semantic binding satisfies it.
The Critic is authoritative only for diagnosis and required_postcondition. Any concrete Interface ID, edit operation, patch wording, source choice, or target choice appearing incidentally in Critic text is non-authoritative. Independently determine the repair from upstream facts and validation evidence. Prefer the smallest coherent SEMANTIC change that fully satisfies all acceptance facts. Completeness and correctness take priority over minimizing edits. 
For deterministic closure failures:

Prefer satisfying missing contractual obligations.

When the issue type is:
- uncovered_required_logical_input
- uncovered_required_platform_output

the repair MUST preserve all valid existing bindings and add the minimum required semantic binding.

Do not redesign existing Interface topology unless an existing binding is proven semantically invalid.

4. AUTHORITY PRIORITY
1. confirmed user requirements
2. frozen FunctionItems and logical-port contracts
3. shared platform / provenance / source-path contracts
4. deterministic validation facts
5. semantic Reviewer / Graph evidence
6. Critic diagnosis and required_postcondition
7. previous Interface Plan

5. CRITIC AUTHORITY

The Critic provides only a diagnosis hypothesis and a required postcondition.

The Critic does NOT override:

- confirmed requirements
- frozen FunctionItems
- logical port contracts
- platform contract
- final_output_fields
- Interface schema


If Critic diagnosis conflicts with any frozen contract,
the frozen contract always wins.

Contract Identifier Preservation Rule:

The repair generator MUST preserve all identifiers declared by upstream
contracts.

This includes:

- platform output fields
- FunctionItem logical inputs
- FunctionItem logical outputs
- Interface source references
- Interface target references


A repair MUST NOT rename, replace, normalize, generalize, or specialize a
declared identifier only because another identifier appears semantically similar
or more descriptive.


If a Critic diagnosis conflicts with an upstream contract identifier:

- the upstream contract is authoritative;
- the Critic diagnosis is considered incomplete or incorrect;
- the repair must preserve the declared identifier and repair only the actual
  invalid relationship.


The Generator independently determines the repair based on authoritative facts.

Do not mechanically execute Critic wording.

6. ACCEPTANCE CONDITIONS
- Do not modify FunctionItems or add fields outside the Interface schema.
- Do not infer relationships from filenames or matching field names alone.
- Do not invent platform inputs merely to close the graph.
- Preserve unrelated logical bindings. Do not change an unrelated semantic source/receiving-slot identity unless necessary for the complete corrected plan.
- Record order, Interface IDs, and goal wording are not semantic preservation requirements.
- Repeated member pairs are allowed when transfers are independent.
- One Interface remains independently bindable to one graph edge.
- Revise Interface structure as necessary within the editable Interface layer.
- Returning an unchanged plan is invalid.
- Use the explicit logical binding fields defined by the Interface schema.
- Do not add opaque endpoint IDs or Graph edges.
- For platform_to_member, source_path is part of the declared logical binding.
      It may be revised when the supplied required postcondition requires a
  different nested platform value.
- Do not invent or modify source_path without support from confirmed requirements
  and platform-input semantics.

7. SELF-CHECK

Before returning:

Verify:

1. Every required receiving slot has one correct semantic source.

2. Every member_to_platform Interface uses a platform output declared by the
platform contract.

3. Platform output identifiers are preserved exactly.

4. No FunctionItem responsibility or logical port is modified.

5. No repair is based only on field-name similarity.

6. No repair changes a valid binding because another name appears clearer.

7. Every blocking semantic issue is actually resolved.

Returning the previous invalid plan is forbidden.

Returning a renamed equivalent without semantic improvement is forbidden.

8. OUTPUT CONTRACT
Return only a patch object matching interface_patch_schema. The only allowed
operations are remove_interface, replace_source, and replace_target. Do not
return or regenerate the complete Interface Plan. This is a strict JSON-schema
response: output exactly one {"operations": [...]} object and no prose. Keep
the entire response concise and every reason at or below 200 characters.

If no valid semantic patch exists, return empty operations. Do not repeat
unchanged values. Do not create a patch that keeps the same source or target.
"""
    payload = {
        "system_goal": original_user_goal,
        "skill_name": skill_name,
        "function_items": _compact_function_items(frozen_function_items),
        "current_interface_plan": current_interface_plan,
        "validation_issues": validation_issues,
        "failed_validation_reason": repair_scope.get("failed_validation_reason", []),
        "legal_member_domain": [
            item["target_file"] for item in _compact_function_items(frozen_function_items)
        ],
        "requirement_allocations": requirement_allocations or [],
        "requirement_channels": requirement_channels or {},
        "unowned_system_requirements": system_requirements_context,
        "platform_contract": platform_contract or {},
        "repair_scope": repair_scope,
        "interface_schema": INTERFACE_SCHEMA,
        "interface_patch_schema": INTERFACE_PATCH_SCHEMA,
        "runtime_binding_facts": build_runtime_binding_facts(function_items=frozen_function_items, platform_contract=platform_contract),
        "critic_schema": CRITIC_SCHEMA,
    }
    critic_call = reviewer_model_call or model_call
    critic_text = await critic_call(
        [{"role": "system", "content": critic_prompt},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        planner_model,
    )
    protocol_repair_used = False
    try:
        critic = validate_interface_repair_critic(
            _parse_object(critic_text), validation_issues=validation_issues,
            current_interface_plan=current_interface_plan, frozen_function_items=frozen_function_items,
            repair_scope=repair_scope,
        )
    except InterfaceIntentPlanError as original_exc:
        protocol_repair_used = True
        reformatter_prompt = """1. RAW RESPONSE
Use the supplied raw Critic response.
2. PROTOCOL ERROR
Use the supplied structured validation error.
3. ALLOWED TRANSPORT REPAIRS
Repair only JSON syntax, exact fields, and array/object boundaries. Preserve
diagnosis and required_postcondition.
4. FORBIDDEN SEMANTIC CHANGES
Do not create, remove, merge, split, or reinterpret repair diagnoses. Do not
choose sources or endpoint IDs. If semantic content is absent, do not invent it.
5. OUTPUT CONTRACT
Return only strict JSON matching critic_schema."""
        repaired_text = await critic_call(
            [{"role": "system", "content": reformatter_prompt},
             {"role": "user", "content": json.dumps({"raw_response": critic_text, "validation_error": {"code": original_exc.code, "details": original_exc.details}, "critic_schema": CRITIC_SCHEMA}, ensure_ascii=False)}],
            planner_model,
        )
        try:
            critic = validate_interface_repair_critic(
                _parse_object(repaired_text), validation_issues=validation_issues,
                current_interface_plan=current_interface_plan, frozen_function_items=frozen_function_items,
                repair_scope=repair_scope,
            )
        except InterfaceIntentPlanError as repair_exc:
            raise InterfaceIntentPlanError(
                "interface repair critic protocol repair failed",
                code="interface_repair_critic_protocol_repair_failed",
                details={"original_error": {"code": original_exc.code, "details": original_exc.details}, "repair_error": {"code": repair_exc.code, "details": repair_exc.details}},
            ) from repair_exc
    logger.info(
        "[Creator][interface_repair_critic] protocol_repair_used=%s diagnosis_present=true objective_present=true",
        protocol_repair_used,
    )
    payload["repair_critic"] = critic
    prompt = f"{prompt}\n\n{REFINEMENT_FEEDBACK_CONTRACT}"
    generator_transport_repair_used = False

    async def propose_generator(previous_candidate: Any, feedback: dict[str, Any]) -> Any:
        nonlocal generator_transport_repair_used
        attempt_payload = {
            **payload,
            "current_interface_plan": previous_candidate,
            "previous_candidate": previous_candidate,
            "refinement_feedback": {
                **feedback,
                "issue_code_set_before": sorted({str(issue.get("code") or "") for issue in validation_issues if issue.get("code")}),
                "issue_code_set_after": sorted({str(issue.get("code") or "") for issue in feedback.get("acceptance_facts", []) if issue.get("code")}),
            },
        }
        text = await model_call(
            [{"role": "system", "content": prompt},
             {"role": "user", "content": json.dumps(attempt_payload, ensure_ascii=False, default=str)}],
            planner_model,
        )
        try:
            patch = validate_interface_patch_protocol(_parse_object(text))
            return apply_interface_patch(
                previous_candidate, patch,
                runtime_binding_facts=payload["runtime_binding_facts"],
            )
        except InterfaceIntentPlanError:
            generator_transport_repair_used = True
            return {"__invalid_transport__": text}

    async def evaluate_generator(candidate_object: Any) -> CandidateEvaluation:
        try:
            candidate = validate_interface_plan_protocol(candidate_object)
        except InterfaceIntentPlanError as protocol_exc:
            return CandidateEvaluation(
                accepted=False, candidate=candidate_object,
                acceptance_facts=[{"code": protocol_exc.code, "message": str(protocol_exc),
                                   "details": protocol_exc.details}],
                semantic_comparable=False,
            )
        validate_interface_repair_scope(
            before=current_interface_plan, after=candidate, repair_scope=repair_scope,
        )
        remaining = collect_interface_plan_validation_issues(
            plan=candidate, function_items=frozen_function_items,
            platform_contract=platform_contract,
        )
        review_issues: list[dict[str, Any]] = []
        if not remaining and reviewer_model:
            review_issues = await review_interface_plan_semantically(
                original_user_goal=original_user_goal,
                frozen_function_items=frozen_function_items,
                interface_plan=candidate,
                requirement_allocations=requirement_allocations,
                requirement_channels=requirement_channels,
                system_requirements=system_requirements_context,
                platform_contract=platform_contract,
                reviewer_model=reviewer_model,
                model_call=reviewer_model_call or model_call,
            )
        graph_issues: list[dict[str, Any]] = []
        if not remaining and not review_issues and repair_stage == "graph_expansion_feedback":
            # Local import avoids the module cycle: graph expansion consumes Interface helpers.
            from .responsibility_graph_expansion import (
                validate_responsibility_graph_candidate,
            )
            try:
                validate_responsibility_graph_candidate(
                    function_items=frozen_function_items,
                    platform_contract=platform_contract or {},
                    interface_plan=candidate,
                )
            except GraphValidationError as graph_exc:
                graph_issues = [{
                    "code": graph_exc.code,
                    "message": str(graph_exc),
                    "details": dict(graph_exc.details or {}),
                    "stage": "graph_validation",
                }]
        residual = merge_interface_validation_issues(
            remaining, review_issues, graph_issues,
        )
        before_issue_codes = sorted({str(issue.get("code")) for issue in validation_issues if issue.get("code")})
        after_issue_codes = sorted({str(issue.get("code")) for issue in residual if issue.get("code")})
        unresolved_old_codes = sorted(set(before_issue_codes) & set(after_issue_codes))
        logger.info(
            "[Creator][interface_repair_acceptance] before_issue_codes=%s after_issue_codes=%s unresolved_old_issue_codes=%s blocking_issue_count=%d",
            before_issue_codes, after_issue_codes, unresolved_old_codes, len(residual),
        )
        return CandidateEvaluation(
            accepted=not residual and not unresolved_old_codes, candidate=candidate,
            acceptance_facts=residual, semantic_comparable=True,
        )

    try:
        candidate = await bounded_refine_candidate(
            stage=repair_stage, initial_candidate=current_interface_plan,
            initial_evaluation=CandidateEvaluation(
                accepted=False, candidate=current_interface_plan,
                acceptance_facts=validation_issues, semantic_comparable=True,
            ),
            propose=propose_generator, evaluate=evaluate_generator,
            semantic_signature=lambda value: (
                canonical_logical_binding_signatures(value),
                interface_receiving_slot_signature(value),
            )
            if isinstance(value, dict) and "interfaces" in value else value,
            max_attempts=2,
        )
    except BoundedRefinementFailed as exc:
        raise InterfaceIntentPlanError(
            "semantic issues remain after repair", code="semantic_issues_remain",
            details={"stage": repair_stage, "repair_attempts": exc.attempt,
                     "semantic_changed": exc.semantic_changed,
                     "original_issues": validation_issues,
                     "remaining_issues": exc.evaluation.acceptance_facts},
        ) from exc
    return validate_interface_intent_plan(plan=candidate, function_items=frozen_function_items)

async def repair_interface_intents(
    *, original_user_goal: str, frozen_function_items: list[dict[str, Any]],
    current_interface_plan: dict[str, Any], validation_errors: list[dict[str, Any]],
    affected_members: list[str] | None = None,
    missing_platform_output_fields: list[str] | None = None,
    requirement_allocations: list[dict[str, Any]] | None = None,
    requirement_channels: dict[str, str] | None = None,
    system_requirements: list[dict[str, Any]] | None = None,
    interaction_requirements: list[dict[str, Any]] | None = None,
    platform_contract: dict[str, Any] | None = None, skill_name: str = "",
    repair_stage: str = "graph_expansion_feedback",
    planner_model: str, model_call: ModelCall, reviewer_model: str | None = None,
    reviewer_model_call: ModelCall | None = None,
) -> dict[str, Any]:
    """Route deterministic graph observations through the semantic repair cycle."""
    failures: list[dict[str, Any]] = []
    uncovered_runtime_inputs: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for error in validation_errors:
        details = dict(error.get("details") or {})
        interface_id = str(details.get("interface_id") or "").strip()
        failure = {
            "code": str(error.get("code") or "graph_validation_failure"),
            "message": str(error.get("message") or "A deterministic graph constraint failed."),
            "details": details,
        }
        failures.append(failure)
        for value in details.get("uncovered_inputs") or []:
            if not isinstance(value, dict):
                continue
            fact = {
                "target_member": str(value.get("target") or value.get("target_member") or ""),
                "target_input": str(value.get("input_id") or value.get("target_input") or ""),
                "required": value.get("required") is not False,
                "default_present": value.get("default_present") is True,
            }
            if fact["target_member"] and fact["target_input"] and fact not in uncovered_runtime_inputs:
                uncovered_runtime_inputs.append(fact)
        envelope = {
            "code": failure["code"],
            "message": failure["message"],
            "affected_interfaces": [interface_id] if interface_id else [],
            "affected_members": [], "affected_inputs": [],
            "evidence": {"observed": details, "constraint": dict(error.get("constraint") or {})},
        }
        issues.append({**envelope, "stage": "graph_validation", "path": "$.interfaces",
                       "interface_id": interface_id, "details": envelope})
    graph_feedback = {
        "stage": "graph_expansion",
        "failures": failures,
        "uncovered_runtime_inputs": uncovered_runtime_inputs,
        "uncovered_platform_outputs": [
            {"output_field": str(value)} for value in (missing_platform_output_fields or [])
        ],
    }
    for issue in issues:
        issue["details"]["graph_feedback"] = graph_feedback
    logger.info(
        "[Creator][graph_feedback] failure_count=%d uncovered_runtime_input_count=%d uncovered_platform_output_count=%d",
        len(failures), len(uncovered_runtime_inputs), len(graph_feedback["uncovered_platform_outputs"]),
    )
    scope = build_interface_repair_scope(issues, current_interface_plan)
    return await repair_interface_plan_semantically(
        original_user_goal=original_user_goal, frozen_function_items=frozen_function_items,
        current_interface_plan=current_interface_plan, validation_issues=issues,
        repair_scope=scope, requirement_allocations=requirement_allocations,
        requirement_channels=requirement_channels, system_requirements=system_requirements,
        interaction_requirements=interaction_requirements, platform_contract=platform_contract,
        skill_name=skill_name, repair_stage=repair_stage, planner_model=planner_model,
        model_call=model_call, reviewer_model=reviewer_model,
        reviewer_model_call=reviewer_model_call,
    )
