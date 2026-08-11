"""Frozen script and Skill-level tool authorities for Creator compilation."""
from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import json
import logging
from typing import Any, Iterable

from ..creator_tool_registry import get_tool_capability, list_tool_capabilities

logger = logging.getLogger(__name__)


def _unique(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _digest(value: dict[str, Any]) -> str:
    facts = {key: item for key, item in value.items() if not key.endswith("_digest")}
    encoded = json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def project_frozen_script_contract(
    *, target_file: str, inputs: Iterable[str], outputs: Iterable[str],
    responsibility: str = "", required_resources: Iterable[str] = (),
) -> dict[str, Any]:
    """Project only what one frozen script must implement; never tool policy."""
    contract = {
        "target_file": target_file,
        "inputs": _unique(inputs),
        "outputs": _unique(outputs),
        "responsibility": str(responsibility or ""),
        "required_resources": _unique(required_resources),
    }
    contract["script_contract_digest"] = _digest(contract)
    return contract


def project_frozen_skill_tool_contract(tool_pool: Any) -> dict[str, Any]:
    """Freeze callable authority from the final, Skill-wide ToolPool.

    The pool decides Tool IDs. Registry function manifests are the sole source
    of import paths, callable names, and signatures. FunctionItems do not enter
    this projection.
    """
    raw_tools = getattr(tool_pool, "tools", None)
    if raw_tools is None and isinstance(tool_pool, dict):
        raw_tools = tool_pool.get("tools")
    allowed_ids: list[str] = []
    callables: list[dict[str, str]] = []
    for raw in raw_tools or []:
        status = getattr(raw, "status", None) if not isinstance(raw, dict) else raw.get("status")
        tool_id = str(getattr(raw, "tool_id", "") if not isinstance(raw, dict) else raw.get("tool_id") or "").strip()
        if status != "allowed" or not tool_id:
            continue
        allowed_ids.append(tool_id)
        registry_tool = get_tool_capability(tool_id)
        if registry_tool is None:
            continue
        for function in registry_tool.functions or []:
            import_path = str(function.import_path or "").strip()
            function_name = str(function.function_name or "").strip()
            if import_path and function_name:
                callables.append({
                    "tool_id": tool_id,
                    "import_path": import_path,
                    "function": function_name,
                    "signature": str(function.signature or "").strip(),
                })
    contract = {
        "allowed_tool_ids": _unique(allowed_ids),
        "allowed_callables": callables,
    }
    contract["skill_tool_contract_digest"] = _digest(contract)
    logger.info(
        "[Creator][frozen_skill_tool_contract] allowed_tool_ids=%s allowed_callable_count=%d skill_tool_contract_digest=%s",
        contract["allowed_tool_ids"], len(callables), contract["skill_tool_contract_digest"],
    )
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


def _run_function(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    return next((node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"), None)


def _registry_callable_domain() -> set[tuple[str, str]]:
    return {
        (str(function.import_path).strip(), str(function.function_name).strip())
        for tool in list_tool_capabilities()
        for function in (tool.functions or [])
        if str(function.import_path).strip() and str(function.function_name).strip()
    }


def validate_script_execution_contract(
    source: str, *, script_contract: dict[str, Any], tool_contract: dict[str, Any],
    check_registry_availability: bool = False,
) -> dict[str, Any]:
    """Hard-gate script IO and selected Skill ToolPool callable authority."""
    target = str(script_contract.get("target_file") or "")
    failures: list[dict[str, Any]] = []
    tree = ast.parse(source)
    run_node = _run_function(tree)
    expected_inputs = set(script_contract.get("inputs") or [])
    expected_outputs = set(script_contract.get("outputs") or [])

    actual_inputs: set[str] | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id == "strict_json_argv_guard")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "strict_json_argv_guard")
        ) and len(node.args) >= 2:
            keys = _literal_dict_keys(node.args[1])
            actual_inputs = set(keys) if keys is not None else None
            break
    if expected_inputs and actual_inputs is None:
        failures.append({"code": "script_input_contract_unverifiable", "target_file": target})
    elif actual_inputs is not None and actual_inputs != expected_inputs:
        missing, unexpected = sorted(expected_inputs - actual_inputs), sorted(actual_inputs - expected_inputs)
        code = "unexpected_script_input" if unexpected and not missing else "script_input_contract_mismatch"
        failures.append({"code": code, "target_file": target, "expected": sorted(expected_inputs), "actual": sorted(actual_inputs), "missing": missing, "unexpected": unexpected})

    output_sets: list[set[str]] = []
    if run_node is not None:
        for node in ast.walk(run_node):
            if isinstance(node, ast.Return) and node.value is not None:
                keys = _literal_dict_keys(node.value)
                if keys is not None:
                    output_sets.append(set(keys))
    actual_outputs = output_sets[0] if output_sets and all(item == output_sets[0] for item in output_sets) else None
    if expected_outputs and actual_outputs is None:
        failures.append({"code": "script_output_contract_unverifiable", "target_file": target})
    elif actual_outputs is not None and actual_outputs != expected_outputs:
        failures.append({
            "code": "script_output_contract_mismatch", "target_file": target,
            "expected": sorted(expected_outputs), "actual": sorted(actual_outputs),
            "missing": sorted(expected_outputs - actual_outputs), "unexpected": sorted(actual_outputs - expected_outputs),
        })

    imports: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imports[alias.asname or alias.name] = (node.module or "", alias.name)
    actual_calls = {imports[node.func.id] for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in imports}
    allowed = {(str(item.get("import_path") or ""), str(item.get("function") or "")) for item in tool_contract.get("allowed_callables") or []}
    registry_owned = _registry_callable_domain()
    for module, function in sorted((actual_calls & registry_owned) - allowed):
        failures.append({"code": "unauthorized_callable", "target_file": target, "actual": f"{module}.{function}", "allowed": [f"{m}.{f}" for m, f in sorted(allowed)]})
    if check_registry_availability:
        for module, function in sorted(allowed):
            try:
                available = importlib.util.find_spec(module) is not None and hasattr(importlib.import_module(module), function)
            except (ImportError, AttributeError, ValueError):
                available = False
            if not available:
                failures.append({"code": "registry_callable_unavailable", "target_file": target, "callable": f"{module}.{function}"})

    codes = [failure["code"] for failure in failures]
    logger.info(
        "[Creator][script_contract_validation] target=%s input_match=%s output_match=%s callable_match=%s failure_codes=%s script_contract_digest=%s skill_tool_contract_digest=%s",
        target, not any("input" in code for code in codes), not any("output" in code for code in codes),
        "unauthorized_callable" not in codes, codes, script_contract.get("script_contract_digest"), tool_contract.get("skill_tool_contract_digest"),
    )
    return {
        "success": not failures, "failures": failures,
        "script_contract_digest": script_contract.get("script_contract_digest"),
        "skill_tool_contract_digest": tool_contract.get("skill_tool_contract_digest"),
    }


def log_downstream_authority(phase: str, target: str, script_contract: dict[str, Any], tool_contract: dict[str, Any]) -> None:
    logger.info(
        "[Creator][downstream_authority] phase=%s target=%s script_contract_digest=%s skill_tool_contract_digest=%s",
        phase, target, script_contract.get("script_contract_digest"), tool_contract.get("skill_tool_contract_digest"),
    )
