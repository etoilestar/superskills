"""Frozen per-file execution authority and deterministic script validation."""
from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import json
import logging
from typing import Any, Iterable

from ..creator_tool_registry import ToolCapability, list_tool_capabilities

logger = logging.getLogger(__name__)


class ExecutionContractError(ValueError):
    def __init__(self, failure: dict[str, Any]):
        self.failure = failure
        super().__init__(json.dumps(failure, ensure_ascii=False, sort_keys=True))


def _unique(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _offered_capabilities(tool: ToolCapability) -> set[str]:
    # Registry metadata is the only capability -> Tool ID authority.  The name
    # fallback is compatibility for existing formal registry records.
    return set(_unique([*(getattr(tool, "capabilities", []) or []), tool.name]))


def _function_fact(function: Any) -> dict[str, str]:
    return {
        "import_path": str(getattr(function, "import_path", "") or "").strip(),
        "function": str(getattr(function, "function_name", "") or "").strip(),
        "signature": str(getattr(function, "signature", "") or "").strip(),
    }


def execution_contract_digest(contract: dict[str, Any]) -> str:
    facts = {key: value for key, value in contract.items() if key != "contract_digest"}
    return hashlib.sha256(json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def project_per_file_execution_contract(
    *, target_file: str, inputs: Iterable[str], outputs: Iterable[str],
    required_capabilities: Iterable[str], forbidden_capabilities: Iterable[str] = (),
    required_resources: Iterable[str] = (), incoming_bindings: Iterable[Any] = (),
    outgoing_bindings: Iterable[Any] = (), registry: Iterable[ToolCapability] | None = None,
) -> dict[str, Any]:
    """Project one immutable downstream authority using exact registry sets."""
    tools = list(registry if registry is not None else list_tool_capabilities())
    required = _unique(required_capabilities)
    forbidden = _unique(forbidden_capabilities)
    domain = set().union(*(_offered_capabilities(tool) for tool in tools)) if tools else set()
    unknown = [capability for capability in required if capability not in domain]
    if unknown:
        raise ExecutionContractError({"code": "unknown_required_capability", "target_file": target_file, "capabilities": unknown})

    allowed_tools: list[dict[str, Any]] = []
    required_set, forbidden_set = set(required), set(forbidden)
    for tool in tools:
        offered = _offered_capabilities(tool)
        if not (offered & required_set) or offered & forbidden_set:
            continue
        callables = [_function_fact(item) for item in (tool.functions or [])]
        callables = [item for item in callables if item["import_path"] and item["function"]]
        allowed_tools.append({"tool_id": tool.name, "capabilities": sorted(offered), "callables": callables})

    if required and not any(tool["callables"] for tool in allowed_tools):
        raise ExecutionContractError({"code": "no_authorized_callable_for_required_capability", "target_file": target_file, "capabilities": required})
    contract = {
        "target_file": target_file, "inputs": _unique(inputs), "outputs": _unique(outputs),
        "required_capabilities": required, "forbidden_capabilities": forbidden,
        "required_resources": _unique(required_resources),
        "incoming_bindings": list(incoming_bindings), "outgoing_bindings": list(outgoing_bindings),
        "allowed_tools": allowed_tools,
    }
    contract["contract_digest"] = execution_contract_digest(contract)
    logger.info("[Creator][per_file_execution_contract] target=%s inputs=%s outputs=%s required_capabilities=%s forbidden_capabilities=%s allowed_tool_ids=%s allowed_callable_count=%d contract_digest=%s", target_file, contract["inputs"], contract["outputs"], required, forbidden, [tool["tool_id"] for tool in allowed_tools], sum(len(tool["callables"]) for tool in allowed_tools), contract["contract_digest"])
    return contract


def _literal_dict_keys(node: ast.AST) -> list[str] | None:
    if not isinstance(node, ast.Dict):
        return None
    keys: list[str] = []
    for key in node.keys:
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return None
        keys.append(key.value)
    return keys


def validate_script_execution_contract(source: str, contract: dict[str, Any], *, check_registry_availability: bool = False) -> dict[str, Any]:
    """Exact-check statically declared argv/stdout fields and registry calls."""
    target = str(contract.get("target_file") or "")
    failures: list[dict[str, Any]] = []
    tree = ast.parse(source)
    actual_inputs: list[str] | None = None
    output_candidates: list[list[str]] = []
    imports: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imports[alias.asname or alias.name] = (node.module or "", alias.name)
        elif isinstance(node, ast.Call) and ((isinstance(node.func, ast.Name) and node.func.id == "strict_json_argv_guard") or (isinstance(node.func, ast.Attribute) and node.func.attr == "strict_json_argv_guard")) and len(node.args) >= 2:
            actual_inputs = _literal_dict_keys(node.args[1])
        elif isinstance(node, ast.Return):
            keys = _literal_dict_keys(node.value) if node.value else None
            if keys is not None:
                output_candidates.append(keys)
    expected_inputs, expected_outputs = list(contract.get("inputs") or []), list(contract.get("outputs") or [])
    if actual_inputs is not None and actual_inputs != expected_inputs:
        code = "unexpected_script_input" if set(expected_inputs) < set(actual_inputs) else "script_input_contract_mismatch"
        failures.append({"code": code, "target_file": target, "expected": expected_inputs, "actual": actual_inputs})
    actual_outputs = output_candidates[-1] if output_candidates else None
    if actual_outputs is not None and actual_outputs != expected_outputs:
        failures.append({"code": "script_output_contract_mismatch", "target_file": target, "expected": expected_outputs, "actual": actual_outputs})
    allowed = {(item["import_path"], item["function"]) for tool in contract.get("allowed_tools") or [] for item in tool.get("callables") or []}
    actual_calls = {imports[node.func.id] for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in imports}
    platform_calls = {pair for pair in actual_calls if pair[0].startswith("backend.services.runtime_tools") and pair[1] != "strict_json_argv_guard"}
    for module, function in sorted(platform_calls - allowed):
        failures.append({"code": "unauthorized_callable", "target_file": target, "actual": f"{module}.{function}", "allowed": [f"{m}.{f}" for m, f in sorted(allowed)]})
    if check_registry_availability:
        for module, function in sorted(allowed):
            try:
                spec = importlib.util.find_spec(module)
                exported = spec is not None and hasattr(importlib.import_module(module), function)
            except (ImportError, AttributeError, ValueError):
                exported = False
            if not exported:
                failures.append({"code": "registry_callable_unavailable", "target_file": target, "callable": f"{module}.{function}"})
    codes = [failure["code"] for failure in failures]
    digest = contract.get("contract_digest") or execution_contract_digest(contract)
    logger.info("[Creator][script_contract_validation] target=%s input_match=%s output_match=%s callable_match=%s failure_codes=%s contract_digest=%s", target, not any("input" in code for code in codes), not any("output" in code for code in codes), "unauthorized_callable" not in codes, codes, digest)
    return {"success": not failures, "failures": failures, "contract_digest": digest}


def log_downstream_authority(phase: str, contract: dict[str, Any]) -> None:
    logger.info("[Creator][downstream_authority] phase=%s target=%s contract_digest=%s", phase, contract.get("target_file"), contract.get("contract_digest"))
