"""Static guard for generated imports from platform runtime helper namespaces."""
from __future__ import annotations
import ast
from typing import Any
from backend.services.runtime_tools import __all__ as RUNTIME_TOOLS_ALL
from .tool_pool_models import RuntimeImportGuardResult, ToolPoolFileBinding

_CUSTOM_PREFIX = 'backend.services.runtime_tools.custom_tools'
_RUNTIME_PREFIX = 'backend.services.runtime_tools'

def _binding_available_tools(binding: ToolPoolFileBinding | dict[str, Any] | None) -> list[dict[str, Any]]:
    if binding is None:
        return []
    raw = getattr(binding, 'available_tools', None) if isinstance(binding, ToolPoolFileBinding) else binding.get('available_tools')
    return [item for item in (raw or []) if isinstance(item, dict)]

def _allowed_imports_from_available_tools(binding: ToolPoolFileBinding | dict[str, Any] | None) -> set[tuple[str, str]]:
    allowed: set[tuple[str, str]] = set()
    for tool in _binding_available_tools(binding):
        import_path = str(tool.get('import_path') or '').strip()
        function_name = str(tool.get('function_name') or '').strip()
        if import_path and function_name:
            allowed.add((import_path, function_name))
    return allowed

def _tools_by_import(binding: ToolPoolFileBinding | dict[str, Any] | None) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(tool.get('import_path') or '').strip(), str(tool.get('function_name') or '').strip()): tool
        for tool in _binding_available_tools(binding)
        if str(tool.get('import_path') or '').strip() and str(tool.get('function_name') or '').strip()
    }

def _required_schema_keys(tool: dict[str, Any]) -> set[str]:
    schema = tool.get('input_schema') if isinstance(tool, dict) else {}
    if not isinstance(schema, dict):
        return set()
    required = schema.get('required')
    return {str(item) for item in required or [] if str(item or '').strip()}

def guard_runtime_imports(source: str, target_file: str, file_binding: ToolPoolFileBinding | dict[str, Any] | None = None) -> RuntimeImportGuardResult:
    allowed_imports = _allowed_imports_from_available_tools(file_binding)
    tools_by_import = _tools_by_import(file_binding)
    allowed_helpers = sorted(function for module, function in allowed_imports if module == _RUNTIME_PREFIX)
    allowed_modules = {module for module, _ in allowed_imports}
    runtime_all = set(RUNTIME_TOOLS_ALL)
    missing: list[str] = []; forbidden: list[str] = []; warnings: list[str] = []
    custom_wildcard: list[str] = []; observed_unbound: list[str] = []
    try:
        tree = ast.parse(source or '')
    except SyntaxError as exc:
        return RuntimeImportGuardResult(success=False, error_type='generated_python_syntax_error', target_file=target_file, allowed_helper_imports=allowed_helpers, repair_instruction=str(exc))
    runtime_aliases: set[str] = set()
    imported_tool_aliases: dict[str, tuple[str, str]] = {}
    schema_errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ''
            if module == _RUNTIME_PREFIX:
                for alias in node.names:
                    name = alias.name
                    if name == '*':
                        forbidden.append('*')
                    elif name not in runtime_all:
                        missing.append(name)
                    elif (module, name) not in allowed_imports:
                        observed_unbound.append(f'{module}.{name}')
                    else:
                        imported_tool_aliases[alias.asname or name] = (module, name)
            elif module.startswith(_CUSTOM_PREFIX) or module.startswith('backend.services.') or module in allowed_modules:
                for alias in node.names:
                    if alias.name == '*':
                        custom_wildcard.append(module)
                    elif (module, alias.name) not in allowed_imports:
                        observed_unbound.append(f'{module}.{alias.name}')
                    else:
                        imported_tool_aliases[alias.asname or alias.name] = (module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name == _RUNTIME_PREFIX:
                    runtime_aliases.add(alias.asname or name.split('.')[-1])
                if (name.startswith(_CUSTOM_PREFIX) or name.startswith('backend.services.')) and name not in allowed_modules:
                    observed_unbound.append(name)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == '__import__':
                if node.args and isinstance(node.args[0], ast.Constant):
                    imported=str(node.args[0].value)
                    if imported == _RUNTIME_PREFIX:
                        warnings.append('__import__ runtime_tools usage is not allowed for generated helpers'); forbidden.append('__import__(backend.services.runtime_tools)')
                    elif imported.startswith(_CUSTOM_PREFIX) and imported not in allowed_modules:
                        forbidden.append(f'__import__({imported})')
            if isinstance(node.func, ast.Name) and node.func.id == 'getattr':
                if len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and node.args[0].id in runtime_aliases and isinstance(node.args[1], ast.Constant):
                    name = str(node.args[1].value)
                    if name not in runtime_all:
                        missing.append(name)
                    elif (_RUNTIME_PREFIX, name) not in allowed_imports:
                        observed_unbound.append(f'{_RUNTIME_PREFIX}.{name}')
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        tool_key = imported_tool_aliases.get(node.func.id)
        if not tool_key:
            continue
        tool = tools_by_import.get(tool_key) or {}
        required_keys = _required_schema_keys(tool)
        if not required_keys:
            continue
        provided = {kw.arg for kw in node.keywords if kw.arg}
        has_kwargs_unpack = any(kw.arg is None for kw in node.keywords)
        if has_kwargs_unpack:
            continue
        missing_keys = sorted(required_keys - provided)
        if missing_keys:
            schema_errors.append(f'{tool_key[0]}.{tool_key[1]} missing required input_schema keys: {", ".join(missing_keys)}')
    missing=sorted(set(missing)); forbidden=sorted(set(forbidden)); custom_wildcard=sorted(set(custom_wildcard)); observed_unbound=sorted(set(observed_unbound)); schema_errors=sorted(set(schema_errors))
    if custom_wildcard:
        return RuntimeImportGuardResult(success=False, error_type='generated_custom_tool_wildcard_import', target_file=target_file, forbidden_imports=custom_wildcard, allowed_helper_imports=allowed_helpers, repair_instruction='Do not use wildcard imports for custom tools; use only current_file_tool_binding.available_tools import_path/function_name pairs.')
    if missing or forbidden:
        err = 'generated_unknown_runtime_tool_import' if missing else 'generated_forbidden_runtime_import_structure'
        return RuntimeImportGuardResult(success=False, error_type=err, target_file=target_file, missing_imports=missing, forbidden_imports=forbidden, allowed_helper_imports=allowed_helpers, suggested_replacements=[], repair_instruction='Runtime import guard only reports mechanical import facts: unknown runtime_tools helpers, wildcard imports, and dynamic runtime imports are not valid platform import structures.', warnings=warnings)
    if observed_unbound:
        return RuntimeImportGuardResult(success=False, error_type='generated_tool_import_not_in_available_tools', target_file=target_file, forbidden_imports=observed_unbound, allowed_helper_imports=allowed_helpers, repair_instruction='Only import functions whose (import_path, function_name) pair exists in current_file_tool_binding.available_tools.', warnings=warnings)
    if schema_errors:
        return RuntimeImportGuardResult(success=False, error_type='generated_tool_call_schema_mismatch', target_file=target_file, forbidden_imports=schema_errors, allowed_helper_imports=allowed_helpers, repair_instruction='Tool call arguments must match the Registry input_schema/signature attached to current_file_tool_binding.available_tools.', warnings=warnings)
    return RuntimeImportGuardResult(success=True, target_file=target_file, allowed_helper_imports=allowed_helpers, warnings=warnings)
