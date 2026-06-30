"""Portable Skill ZIP dependency collection and runtime bundling."""

from __future__ import annotations

import ast
import importlib.metadata
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from zipfile import ZipFile

PORTABILITY_VERSION = "1.1"
RUNTIME_DIR = "_portable_runtime"
REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_TOOLS_DIR = REPO_ROOT / "backend" / "services" / "runtime_tools"
SKILL_RUNTIME_FILE = REPO_ROOT / "backend" / "services" / "skill_runtime.py"

DEFAULT_ENV_PLACEHOLDERS = {
    "OUTPUT_DIR": "./outputs",
    "INPUT_DIR": "./inputs",
    "LLM_API_BASE": "<FILL_ME>",
    "LLM_API_KEY": "<FILL_ME>",
    "LLM_MODEL": "<FILL_ME>",
    "IMAGE_API_BASE": "<FILL_ME>",
    "IMAGE_API_KEY": "<FILL_ME>",
}
_ENV_ALIASES = {
    "LLM_BASE_URL": "LLM_API_BASE",
    "TEXT_MODEL": "LLM_MODEL",
    "IMAGE_BASE_URL": "IMAGE_API_BASE",
    "IMAGE_MODEL": "IMAGE_MODEL",
}
SERVICE_ENV_BY_TOOL = {
    "generate_text_with_llm": {"LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL"},
    "generate_image": {"IMAGE_API_BASE", "IMAGE_API_KEY", "IMAGE_MODEL"},
    "generate_stable_diffusion_image": {"IMAGE_API_BASE", "IMAGE_API_KEY", "IMAGE_MODEL", "LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL"},
    "web_search": {"SEARCHXNG_BASE_URL", "SEARCHXNG_API_KEY", "SEARCHXNG_ENGINE", "SEARCHXNG_TIMEOUT"},
    "query_database_readonly": {"DATABASE_URL", "DB_DIALECT", "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_PORT"},
    "describe_database_table": {"DATABASE_URL", "DB_DIALECT", "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_PORT"},
    "list_database_tables": {"DATABASE_URL", "DB_DIALECT", "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_PORT"},
    "registered_tool_call": {"REGISTERED_TOOL_API_BASE", "REGISTERED_TOOL_API_KEY"},
    "create_wechat_draft": {"WECHAT_APP_ID", "WECHAT_APP_SECRET"},
    "publish_wechat_draft": {"WECHAT_APP_ID", "WECHAT_APP_SECRET"},
    "upload_wechat_media": {"WECHAT_APP_ID", "WECHAT_APP_SECRET"},
}


@dataclass
class PortableReport:
    system_tools_used: list[str] = field(default_factory=list)
    copied_modules: list[str] = field(default_factory=list)
    copied_runtime_files: list[str] = field(default_factory=list)
    third_party_imports: list[str] = field(default_factory=list)
    third_party_requirements: list[str] = field(default_factory=list)
    unresolved_requirements: list[str] = field(default_factory=list)
    env_vars_used: list[str] = field(default_factory=list)
    env_placeholders: dict[str, str] = field(default_factory=dict)
    patched_scripts: list[str] = field(default_factory=list)
    patched_imports: list[dict[str, str]] = field(default_factory=list)
    adapted_tools: list[str] = field(default_factory=list)
    unsupported_tools: list[str] = field(default_factory=list)
    missing_runtime_modules: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    used_runtime_tools: list[str] = field(default_factory=list)
    used_skill_runtime_tools: list[str] = field(default_factory=list)
    portable_style: str = "inline"
    inlined_tools: list[str] = field(default_factory=list)
    inlined_helpers: list[str] = field(default_factory=list)
    inlined_constants: list[str] = field(default_factory=list)
    inlined_classes: list[str] = field(default_factory=list)
    inline_blocks: list[dict[str, Any]] = field(default_factory=list)
    fallback_to_package: bool = False
    fallback_reasons: list[str] = field(default_factory=list)
    fallback_tools: list[str] = field(default_factory=list)
    fallback_modules: list[str] = field(default_factory=list)

    def manifest(self) -> dict[str, Any]:
        return {
            "portability_version": PORTABILITY_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "runtime_bundle_dir": RUNTIME_DIR,
            "portable_style": "mixed" if self.fallback_to_package and self.portable_style == "inline" else self.portable_style,
            "inlined_tools": self.inlined_tools,
            "inlined_helpers": self.inlined_helpers,
            "inlined_constants": self.inlined_constants,
            "inlined_classes": self.inlined_classes,
            "inline_blocks": self.inline_blocks,
            "fallback_to_package": self.fallback_to_package,
            "fallback_reasons": self.fallback_reasons,
            "fallback_tools": self.fallback_tools,
            "fallback_modules": self.fallback_modules,
            "system_tools_used": self.system_tools_used,
            "patched_imports": self.patched_imports,
            "copied_modules": self.copied_modules,
            "copied_runtime_files": self.copied_runtime_files,
            "adapted_tools": self.adapted_tools,
            "unsupported_tools": self.unsupported_tools,
            "missing_runtime_modules": self.missing_runtime_modules,
            "third_party_requirements": self.third_party_requirements,
            "unresolved_requirements": self.unresolved_requirements,
            "env_vars_used": self.env_vars_used,
            "env_placeholders": self.env_placeholders,
            "requirements_file": "requirements-portable.txt",
            "env_example_file": ".env.example",
            "warnings": self.warnings,
            "dependency_report": {
                "third_party_imports": self.third_party_imports,
                "patched_scripts": self.patched_scripts,
            },
        }


def _stdlib_roots() -> set[str]:
    return set(getattr(sys, "stdlib_module_names", set())) | {"__future__", "typing"}


def _local_module_roots(skill_dir: Path) -> set[str]:
    roots = {RUNTIME_DIR, "_portable_runtime"}
    scripts_dir = skill_dir / "scripts"
    if scripts_dir.exists():
        for path in scripts_dir.rglob("*.py"):
            roots.add(path.stem)
            rel = path.relative_to(scripts_dir).with_suffix("")
            if rel.parts:
                roots.add(rel.parts[0])
    return roots


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return None


def _runtime_export_map() -> dict[str, str]:
    tree = _parse(RUNTIME_TOOLS_DIR / "__init__.py")
    mapping: dict[str, str] = {}
    if tree is None:
        return mapping
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            module = f"backend.services.runtime_tools.{node.module}"
            for alias in node.names:
                mapping[alias.asname or alias.name] = module
    return mapping


def _script_system_imports(skill_dir: Path) -> tuple[set[str], set[str], set[str], set[str], list[tuple[Path, ast.Module]]]:
    runtime_names: set[str] = set()
    runtime_modules: set[str] = set()
    skill_runtime_names: set[str] = set()
    third_party_roots: set[str] = set()
    parsed_scripts: list[tuple[Path, ast.Module]] = []
    local_roots = _local_module_roots(skill_dir)
    for script in sorted((skill_dir / "scripts").rglob("*.py")):
        tree = _parse(script)
        if tree is None:
            continue
        parsed_scripts.append((script, tree))
        for root in _import_roots(tree):
            if _is_third_party(root, local_roots):
                third_party_roots.add(root)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module or node.level != 0:
                continue
            if node.module == "backend.services.runtime_tools":
                runtime_names.update(alias.name for alias in node.names if alias.name != "*")
            elif node.module.startswith("backend.services.runtime_tools."):
                runtime_modules.add(node.module)
                runtime_names.update(alias.name for alias in node.names if alias.name != "*")
            elif node.module == "backend.services.skill_runtime":
                skill_runtime_names.update(alias.name for alias in node.names if alias.name != "*")
    return runtime_names, runtime_modules, skill_runtime_names, third_party_roots, parsed_scripts


def _runtime_module_file(module: str) -> Path | None:
    prefix = "backend.services.runtime_tools."
    if not module.startswith(prefix):
        return None
    rel = module.removeprefix(prefix).replace(".", "/") + ".py"
    path = RUNTIME_TOOLS_DIR / rel
    if path.is_file() and path.resolve().is_relative_to(RUNTIME_TOOLS_DIR.resolve()):
        return path
    return None


def _module_name_for_file(path: Path) -> str:
    if path == SKILL_RUNTIME_FILE:
        return "backend.services.skill_runtime"
    try:
        rel = path.relative_to(REPO_ROOT).with_suffix("")
        return ".".join(rel.parts)
    except ValueError:
        if path.parent == RUNTIME_TOOLS_DIR:
            return f"backend.services.runtime_tools.{path.stem}"
        return path.stem


def _discover_runtime_closure(runtime_names: set[str], runtime_modules: set[str], skill_runtime_names: set[str], report: PortableReport) -> set[Path]:
    export_map = _runtime_export_map()
    files: set[Path] = set()
    queue: list[Path] = []

    def add(path: Path | None, module_name: str | None = None):
        if path is None:
            if module_name:
                report.missing_runtime_modules.append(module_name)
            return
        if path not in files:
            files.add(path)
            queue.append(path)

    for module in sorted(runtime_modules):
        add(_runtime_module_file(module), module)
    for name in sorted(runtime_names):
        add(_runtime_module_file(export_map.get(name, "")), name)
    if skill_runtime_names:
        add(SKILL_RUNTIME_FILE, "backend.services.skill_runtime")
        # skill_runtime imports the runtime_tools facade broadly; copy direct runtime modules so imports remain real.
        for path in sorted(RUNTIME_TOOLS_DIR.glob("*.py")):
            if path.name != "__init__.py":
                add(path, _module_name_for_file(path))
    while queue:
        path = queue.pop(0)
        tree = _parse(path)
        if tree is None:
            report.warnings.append(f"Could not parse runtime module {path}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 1 and path.parent == RUNTIME_TOOLS_DIR and node.module:
                    add(RUNTIME_TOOLS_DIR / f"{node.module}.py", f"backend.services.runtime_tools.{node.module}")
                elif node.level == 1 and path == SKILL_RUNTIME_FILE and node.module == "runtime_tools":
                    for dep in sorted(RUNTIME_TOOLS_DIR.glob("*.py")):
                        if dep.name != "__init__.py":
                            add(dep, _module_name_for_file(dep))
                elif node.module and node.module.startswith("backend.services.runtime_tools."):
                    add(_runtime_module_file(node.module), node.module)
    return files


def _import_roots(tree: ast.AST) -> list[str]:
    roots: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.append(node.module.split(".")[0])
    return roots


def _env_vars(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Attribute) and func.attr in {"get", "getenv"} and node.args:
                if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    name = node.args[0].value
            elif isinstance(func, ast.Name) and func.id in {"_env", "_required_env"} and node.args:
                if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    name = node.args[0].value
            if name:
                found.add(_ENV_ALIASES.get(name, name))
        elif isinstance(node, ast.Subscript):
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                found.add(_ENV_ALIASES.get(node.slice.value, node.slice.value))
    return found


def _is_third_party(root: str, local_roots: set[str]) -> bool:
    return bool(root and root not in local_roots and root != "backend" and root not in _stdlib_roots())


def _requirement_for_import(root: str) -> tuple[str, bool]:
    try:
        dists = importlib.metadata.packages_distributions().get(root) or []
        if dists:
            dist = dists[0]
            return f"{dist}=={importlib.metadata.version(dist)}", True
    except Exception:
        pass
    return root, False


def _placeholders(envs: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in sorted(envs):
        result[name] = DEFAULT_ENV_PLACEHOLDERS.get(name, "<FILL_ME>")
    return result


def collect_portable_dependencies(skill_dir: str | Path, portable_style: Literal["inline", "package"] = "inline") -> PortableReport:
    skill_dir = Path(skill_dir)
    report = PortableReport(portable_style=portable_style)
    runtime_names, runtime_modules, skill_runtime_names, third_party, parsed_scripts = _script_system_imports(skill_dir)
    copied_files = _discover_runtime_closure(runtime_names, runtime_modules, skill_runtime_names, report)
    envs = set(DEFAULT_ENV_PLACEHOLDERS)
    local_roots = _local_module_roots(skill_dir)

    for _script, tree in parsed_scripts:
        envs.update(_env_vars(tree))
    for path in copied_files:
        tree = _parse(path)
        if tree is None:
            continue
        envs.update(_env_vars(tree))
        for root in _import_roots(tree):
            if _is_third_party(root, local_roots):
                third_party.add(root)

    used = sorted(runtime_names | skill_runtime_names)
    report.used_runtime_tools = sorted(runtime_names)
    report.used_skill_runtime_tools = sorted(skill_runtime_names)
    report.system_tools_used = [f"backend.services.runtime_tools:{name}" for name in sorted(runtime_names)]
    if skill_runtime_names:
        report.system_tools_used.extend(f"backend.services.skill_runtime:{name}" for name in sorted(skill_runtime_names))
    adapted = sorted(name for name in used if name in SERVICE_ENV_BY_TOOL)
    report.adapted_tools = adapted
    for name in adapted:
        envs.update(SERVICE_ENV_BY_TOOL[name])
    report.copied_modules = sorted(_module_name_for_file(p) for p in copied_files)
    report.copied_runtime_files = sorted(_portable_arc_for_source(p) for p in copied_files)
    # package skeleton/facades
    report.copied_runtime_files.extend([
        f"{RUNTIME_DIR}/__init__.py",
        f"{RUNTIME_DIR}/backend/__init__.py",
        f"{RUNTIME_DIR}/backend/services/__init__.py",
        f"{RUNTIME_DIR}/backend/services/runtime_tools/__init__.py",
        f"{RUNTIME_DIR}/runtime_tools.py",
        f"{RUNTIME_DIR}/skill_runtime.py",
        f"{RUNTIME_DIR}/env.py",
        f"{RUNTIME_DIR}/bootstrap.py",
    ])
    report.copied_runtime_files = sorted(set(report.copied_runtime_files))
    report.third_party_imports = sorted(third_party)
    seen_req: set[str] = set()
    for root in report.third_party_imports:
        req, resolved = _requirement_for_import(root)
        if req not in seen_req:
            seen_req.add(req)
            report.third_party_requirements.append(req)
        if not resolved:
            report.unresolved_requirements.append(root)
    report.env_placeholders = _placeholders(envs)
    report.env_vars_used = sorted(report.env_placeholders)
    return report


def patch_script_imports(source: str, rel_path: str, report: PortableReport, portable_style: Literal["inline", "package"] = "package") -> str:
    tree = ast.parse(source)
    lines = source.splitlines()
    replacements: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module or node.level != 0:
            continue
        target = None
        if node.module.startswith("_portable_runtime"):
            continue
        if node.module == "backend.services.runtime_tools" or node.module.startswith("backend.services.runtime_tools."):
            target = "_portable_runtime.backend.services.runtime_tools"
        elif node.module == "backend.services.skill_runtime":
            target = "_portable_runtime.backend.services.skill_runtime"
        if not target:
            continue
        imported = ", ".join(alias.name + (f" as {alias.asname}" if alias.asname else "") for alias in node.names)
        fallback = f"try:\n    from {target} import {imported}\nexcept ImportError:\n    from {node.module} import {imported}"
        replacements.append((node.lineno - 1, getattr(node, "end_lineno", node.lineno), fallback))
        report.patched_imports.append({"script": rel_path, "from": node.module, "to": target})
    if not replacements:
        return source
    for start, end, text in sorted(replacements, reverse=True):
        lines[start:end] = text.splitlines()
    if rel_path not in report.patched_scripts:
        report.patched_scripts.append(rel_path)
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")


def _portable_arc_for_source(path: Path) -> str:
    if path == SKILL_RUNTIME_FILE:
        return f"{RUNTIME_DIR}/backend/services/skill_runtime.py"
    rel = path.relative_to(RUNTIME_TOOLS_DIR)
    return f"{RUNTIME_DIR}/backend/services/runtime_tools/{rel.as_posix()}"


def _runtime_init(report: PortableReport) -> str:
    export_map = _runtime_export_map()
    by_module: dict[str, list[str]] = {}
    if "backend.services.skill_runtime" in report.copied_modules:
        # Full facade needed by copied skill_runtime.py.
        return (RUNTIME_TOOLS_DIR / "__init__.py").read_text(encoding="utf-8")
    for name in report.used_runtime_tools:
        module = export_map.get(name)
        if module:
            by_module.setdefault(module.rsplit(".", 1)[-1], []).append(name)
    lines = ['"""Portable runtime_tools facade re-exporting copied real implementations."""', ""]
    all_names: list[str] = []
    for module, names in sorted(by_module.items()):
        joined = ", ".join(sorted(names))
        lines.append(f"from .{module} import {joined}")
        all_names.extend(names)
    lines.append("")
    lines.append(f"__all__ = {sorted(all_names)!r}")
    lines.append("")
    return "\n".join(lines)


def _top_facade(module: str, names: list[str]) -> str:
    if not names:
        return f'"""Portable {module} facade."""\n'
    joined = ", ".join(sorted(names))
    return f'"""Portable {module} facade."""\nfrom .backend.services.{module} import {joined}\n__all__ = {sorted(names)!r}\n'


def _portable_skill_runtime_facade(report: PortableReport) -> str:
    return _top_facade("skill_runtime", report.used_skill_runtime_tools)


def _portable_runtime_tools_facade(report: PortableReport) -> str:
    return _top_facade("runtime_tools", report.used_runtime_tools)


def _patched_skill_runtime_source() -> str:
    source = SKILL_RUNTIME_FILE.read_text(encoding="utf-8")
    aliases = """
def _required_env(name: str) -> str:
    alias_values = {
        "LLM_BASE_URL": ("LLM_BASE_URL", "LLM_API_BASE"),
        "TEXT_MODEL": ("TEXT_MODEL", "LLM_MODEL"),
        "IMAGE_BASE_URL": ("IMAGE_BASE_URL", "IMAGE_API_BASE"),
    }
    names = alias_values.get(name, (name,))
    for candidate in names:
        value = _env(candidate).strip()
        if value and value != "<FILL_ME>":
            return value
    raise RuntimeError(
        f"Portable Skill runtime requires environment variable {name}"
        + (f" (or {', '.join(names[1:])})" if len(names) > 1 else "")
        + "; fill .env.example placeholders or configure a host adapter."
    )
""".strip()
    tree = ast.parse(source)
    lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_required_env":
            lines[node.lineno - 1:getattr(node, "end_lineno", node.lineno)] = aliases.splitlines()
            break
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")



@dataclass
class SourceModuleIndex:
    module: str
    path: Path
    tree: ast.Module
    imports: list[ast.stmt]
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    classes: dict[str, ast.ClassDef]
    assignments: dict[str, ast.Assign | ast.AnnAssign]
    constants: set[str]


@dataclass
class ToolDefinition:
    public_name: str
    module: str
    path: Path
    symbol_name: str
    node: ast.AST


@dataclass
class InlineClosure:
    imports: list[ast.stmt] = field(default_factory=list)
    assignments: dict[str, ast.Assign | ast.AnnAssign] = field(default_factory=dict)
    classes: dict[str, ast.ClassDef] = field(default_factory=dict)
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = field(default_factory=dict)
    entry_symbols: set[str] = field(default_factory=set)
    modules: set[str] = field(default_factory=set)
    third_party_roots: set[str] = field(default_factory=set)
    env_vars: set[str] = field(default_factory=set)
    fallback_reasons: list[str] = field(default_factory=list)


def _build_source_index(module_name: str, path: Path) -> SourceModuleIndex:
    tree = _parse(path)
    if tree is None:
        raise ValueError(f"Could not parse source module {module_name}")
    imports: list[ast.stmt] = []
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    classes: dict[str, ast.ClassDef] = {}
    assignments: dict[str, ast.Assign | ast.AnnAssign] = {}
    constants: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = node
        elif isinstance(node, ast.ClassDef):
            classes[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node
                    if target.id.isupper() or target.id.startswith("_"):
                        constants.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assignments[node.target.id] = node
            if node.target.id.isupper() or node.target.id.startswith("_"):
                constants.add(node.target.id)
    return SourceModuleIndex(module_name, path, tree, imports, functions, classes, assignments, constants)


def _resolve_tool_definition(tool_name: str, import_module: str) -> ToolDefinition | None:
    if import_module == "backend.services.runtime_tools":
        module = _runtime_export_map().get(tool_name)
    elif import_module.startswith("backend.services.runtime_tools."):
        module = import_module
    elif import_module == "backend.services.skill_runtime":
        module = import_module
    else:
        module = None
    if not module:
        return None
    path = SKILL_RUNTIME_FILE if module == "backend.services.skill_runtime" else _runtime_module_file(module)
    if path is None or not path.is_file():
        return None
    index = _build_source_index(module, path)
    node = index.functions.get(tool_name) or index.classes.get(tool_name) or index.assignments.get(tool_name)
    if node is None:
        return None
    return ToolDefinition(tool_name, module, path, tool_name, node)


def _detect_dynamic_fallback(tree: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            return "wildcard import"
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec"}:
                return f"dynamic {node.func.id} call"
            if isinstance(node.func, ast.Attribute) and node.func.attr == "import_module":
                return "dynamic import_module call"
    return None


def _names_loaded(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _import_alias_names(node: ast.stmt) -> set[str]:
    if isinstance(node, ast.Import):
        return {(alias.asname or alias.name.split(".", 1)[0]) for alias in node.names}
    if isinstance(node, ast.ImportFrom):
        return {(alias.asname or alias.name) for alias in node.names if alias.name != "*"}
    return set()


def _relative_runtime_import_module(index: SourceModuleIndex, node: ast.ImportFrom) -> str | None:
    if node.level == 1 and index.module.startswith("backend.services.runtime_tools") and node.module:
        return f"backend.services.runtime_tools.{node.module}"
    if node.module and node.module.startswith("backend.services.runtime_tools."):
        return node.module
    return None


def _collect_local_symbol_closure(index: SourceModuleIndex, entry_symbols: set[str]) -> InlineClosure:
    closure = InlineClosure(entry_symbols=set(entry_symbols), modules={index.module})
    reason = _detect_dynamic_fallback(index.tree)
    if reason:
        closure.fallback_reasons.append(f"{index.module}: {reason}")
        return closure
    visited: set[tuple[str, str]] = set()

    def add_symbol(symbol: str, current: SourceModuleIndex):
        key = (current.module, symbol)
        if key in visited:
            return
        visited.add(key)
        node = current.functions.get(symbol) or current.classes.get(symbol) or current.assignments.get(symbol)
        if node is None:
            return
        closure.modules.add(current.module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            closure.functions.setdefault(symbol, node)
        elif isinstance(node, ast.ClassDef):
            closure.classes.setdefault(symbol, node)
        else:
            closure.assignments.setdefault(symbol, node)
        closure.env_vars.update(_env_vars(node))
        loaded = _names_loaded(node)
        for dep in sorted(loaded):
            if dep in current.functions or dep in current.classes or dep in current.assignments:
                add_symbol(dep, current)
        for imp in current.imports:
            aliases = _import_alias_names(imp)
            if not (aliases & loaded):
                continue
            if isinstance(imp, ast.ImportFrom):
                dep_module = _relative_runtime_import_module(current, imp)
                if dep_module:
                    dep_path = _runtime_module_file(dep_module)
                    if dep_path is None:
                        closure.fallback_reasons.append(f"missing runtime import {dep_module}")
                        continue
                    dep_index = _build_source_index(dep_module, dep_path)
                    for alias in imp.names:
                        if alias.name == "*":
                            closure.fallback_reasons.append(f"wildcard import in {current.module}")
                            continue
                        local = alias.asname or alias.name
                        if local in loaded:
                            add_symbol(alias.name, dep_index)
                    continue
            closure.imports.append(imp)
            for root in _import_roots(ast.Module(body=[imp], type_ignores=[])):
                if root not in _stdlib_roots() and root != "backend":
                    closure.third_party_roots.add(root)

    for symbol in sorted(entry_symbols):
        add_symbol(symbol, index)
    # Deduplicate imports by source text.
    unique: dict[str, ast.stmt] = {}
    for imp in closure.imports:
        try:
            unique[ast.unparse(imp)] = imp
        except Exception:
            pass
    closure.imports = list(unique.values())
    return closure


class _RenameTransformer(ast.NodeTransformer):
    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping

    def visit_Name(self, node: ast.Name):
        if node.id in self.mapping:
            return ast.copy_location(ast.Name(id=self.mapping[node.id], ctx=node.ctx), node)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef):
        if node.name in self.mapping:
            node.name = self.mapping[node.name]
        return self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        if node.name in self.mapping:
            node.name = self.mapping[node.name]
        return self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        if node.name in self.mapping:
            node.name = self.mapping[node.name]
        return self.generic_visit(node)


def _detect_top_level_names(script_ast: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in script_ast.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(_import_alias_names(node))
    return names


def _resolve_inline_name_conflicts(closure: InlineClosure, script_names: set[str], public_names: set[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    all_inline = set(closure.functions) | set(closure.classes) | set(closure.assignments)
    for name in sorted(all_inline):
        if name in public_names:
            continue
        if name in script_names:
            mapping[name] = f"_portable_{name.lstrip('_')}"
    if not mapping:
        return mapping
    transformer = _RenameTransformer(mapping)
    closure.functions = {mapping.get(k, k): transformer.visit(ast.fix_missing_locations(v)) for k, v in closure.functions.items()}
    closure.classes = {mapping.get(k, k): transformer.visit(ast.fix_missing_locations(v)) for k, v in closure.classes.items()}
    closure.assignments = {mapping.get(k, k): transformer.visit(ast.fix_missing_locations(v)) for k, v in closure.assignments.items()}
    closure.entry_symbols = {mapping.get(k, k) for k in closure.entry_symbols}
    return mapping


def _render_inline_tools_block(closure: InlineClosure, report: PortableReport) -> str:
    lines = [
        "# --- BEGIN PORTABLE INLINE TOOLS ---",
        "# generated by superskills portable exporter",
        f"# tools: {', '.join(sorted(report.inlined_tools))}",
        f"# helpers: {', '.join(sorted(report.inlined_helpers))}",
        f"# constants: {', '.join(sorted(report.inlined_constants))}",
    ]
    for node in closure.imports:
        lines.append(ast.unparse(node))
    if closure.imports:
        lines.append("")
    for name in sorted(closure.assignments):
        lines.append(ast.unparse(closure.assignments[name]))
        lines.append("")
    for name in sorted(closure.classes):
        lines.append(ast.unparse(closure.classes[name]))
        lines.append("")
    for name in sorted(closure.functions):
        lines.append(ast.unparse(closure.functions[name]))
        lines.append("")
    lines.append("# --- END PORTABLE INLINE TOOLS ---")
    return "\n".join(lines).rstrip() + "\n"


def _strip_existing_inline_block(source: str) -> str:
    start = "# --- BEGIN PORTABLE INLINE TOOLS ---"
    end = "# --- END PORTABLE INLINE TOOLS ---"
    if start not in source:
        return source
    before, rest = source.split(start, 1)
    if end not in rest:
        return source
    _old, after = rest.split(end, 1)
    return before.rstrip() + "\n" + after.lstrip("\n")


def _insert_inline_block(source: str, block: str) -> str:
    source = _strip_existing_inline_block(source)
    tree = ast.parse(source)
    lines = source.splitlines()
    insert_at = 0
    if lines and lines[0].startswith("#!"):
        insert_at = 1
    if len(lines) > insert_at and ("coding" in lines[insert_at] or "encoding" in lines[insert_at]):
        insert_at += 1
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        insert_at = max(insert_at, getattr(body[0], "end_lineno", body[0].lineno))
    for node in body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            insert_at = max(insert_at, getattr(node, "end_lineno", node.lineno))
    lines[insert_at:insert_at] = ["", *block.rstrip().splitlines(), ""]
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")


def _remove_system_imports(source: str) -> str:
    tree = ast.parse(source)
    lines = source.splitlines()
    remove: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == "backend.services.runtime_tools" or node.module.startswith("backend.services.runtime_tools.") or node.module == "backend.services.skill_runtime":
                remove.append((node.lineno - 1, getattr(node, "end_lineno", node.lineno)))
    for start, end in sorted(remove, reverse=True):
        del lines[start:end]
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")


def _adapter_source(name: str) -> str | None:
    envs = sorted(SERVICE_ENV_BY_TOOL.get(name, []))
    if not envs:
        return None
    return f"""def {name}(*args, **kwargs):\n    required = {envs!r}\n    missing = [name for name in required if not os.environ.get(name) or os.environ.get(name) == '<FILL_ME>']\n    if missing:\n        raise RuntimeError('Portable adapter for {name} requires environment variables: ' + ', '.join(missing) + '; fill .env.example or configure a host adapter.')\n    raise RuntimeError('Portable adapter for {name} needs a host implementation configured via environment variables.')\n"""


def _inline_script_source(source: str, rel_path: str, report: PortableReport) -> tuple[str, bool]:
    tree = ast.parse(source)
    imports: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == "backend.services.runtime_tools" or node.module.startswith("backend.services.runtime_tools.") or node.module == "backend.services.skill_runtime":
                for alias in node.names:
                    if alias.name != "*":
                        imports.append((alias.name, node.module))
    if not imports:
        return source, False
    closure = InlineClosure()
    public_names = {name for name, _module in imports}
    adapter_nodes: dict[str, ast.FunctionDef] = {}
    fallback = False
    for name, module in imports:
        if name in SERVICE_ENV_BY_TOOL:
            adapter = _adapter_source(name)
            if adapter:
                adapter_nodes[name] = ast.parse(adapter).body[0]
                closure.imports.append(ast.parse("import os").body[0])
                report.adapted_tools.append(name)
                report.env_placeholders.update(_placeholders(SERVICE_ENV_BY_TOOL[name]))
                continue
        definition = _resolve_tool_definition(name, module)
        if definition is None:
            fallback = True
            report.fallback_reasons.append(f"could not resolve {module}.{name}")
            report.fallback_tools.append(name)
            report.fallback_modules.append(module)
            continue
        index = _build_source_index(definition.module, definition.path)
        part = _collect_local_symbol_closure(index, {definition.symbol_name})
        if part.fallback_reasons:
            fallback = True
            report.fallback_reasons.extend(part.fallback_reasons)
            report.fallback_tools.append(name)
            report.fallback_modules.append(definition.module)
            continue
        closure.imports.extend(part.imports)
        closure.assignments.update(part.assignments)
        closure.classes.update(part.classes)
        closure.functions.update(part.functions)
        closure.entry_symbols.update(part.entry_symbols)
        closure.modules.update(part.modules)
        closure.third_party_roots.update(part.third_party_roots)
        closure.env_vars.update(part.env_vars)
    if fallback:
        report.fallback_to_package = True
        return source, False
    closure.functions.update(adapter_nodes)
    closure.entry_symbols.update(adapter_nodes)
    closure.env_vars.update(report.env_placeholders)
    script_names = _detect_top_level_names(tree) - public_names
    _resolve_inline_name_conflicts(closure, script_names, public_names)
    report.inlined_tools.extend(sorted(public_names))
    helpers = (set(closure.functions) | set(closure.classes) | set(closure.assignments)) - public_names
    report.inlined_helpers.extend(sorted(set(closure.functions) - public_names))
    report.inlined_classes.extend(sorted(set(closure.classes) - public_names))
    report.inlined_constants.extend(sorted(set(closure.assignments)))
    for root in sorted(closure.third_party_roots):
        if root not in report.third_party_imports:
            report.third_party_imports.append(root)
            req, resolved = _requirement_for_import(root)
            if req not in report.third_party_requirements:
                report.third_party_requirements.append(req)
            if not resolved:
                report.unresolved_requirements.append(root)
    report.env_placeholders.update(_placeholders(closure.env_vars | set(DEFAULT_ENV_PLACEHOLDERS)))
    report.env_vars_used = sorted(report.env_placeholders)
    block = _render_inline_tools_block(closure, report)
    stripped = _remove_system_imports(source)
    report.inline_blocks.append({"script": rel_path, "tools": sorted(public_names), "helpers": sorted(helpers)})
    report.patched_scripts.append(rel_path)
    return _insert_inline_block(stripped, block), True


def portable_runtime_files(report: PortableReport) -> dict[str, str]:
    files = {
        f"{RUNTIME_DIR}/__init__.py": '"""Portable runtime bundled with an exported Skill."""\n',
        f"{RUNTIME_DIR}/backend/__init__.py": "\n",
        f"{RUNTIME_DIR}/backend/services/__init__.py": "\n",
        f"{RUNTIME_DIR}/backend/services/runtime_tools/__init__.py": _runtime_init(report),
        f"{RUNTIME_DIR}/runtime_tools.py": _portable_runtime_tools_facade(report),
        f"{RUNTIME_DIR}/skill_runtime.py": _portable_skill_runtime_facade(report),
        f"{RUNTIME_DIR}/env.py": "from __future__ import annotations\nimport os\ndef get(name, default=''):\n    return os.environ.get(name, default)\n",
        f"{RUNTIME_DIR}/bootstrap.py": "from __future__ import annotations\nimport sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n",
    }
    for module in report.copied_modules:
        if module == "backend.services.skill_runtime":
            files[f"{RUNTIME_DIR}/backend/services/skill_runtime.py"] = _patched_skill_runtime_source()
            continue
        if module.startswith("backend.services.runtime_tools."):
            name = module.rsplit(".", 1)[-1]
            src = RUNTIME_TOOLS_DIR / f"{name}.py"
            if src.is_file():
                files[f"{RUNTIME_DIR}/backend/services/runtime_tools/{name}.py"] = src.read_text(encoding="utf-8")
    return files


def env_example(report: PortableReport) -> str:
    return "".join(f"{name}={value}\n" for name, value in sorted(report.env_placeholders.items()))


def add_package_portable_files_to_zip(zipf: ZipFile, skill_dir: Path, arc_prefix: str = "") -> PortableReport:
    report = collect_portable_dependencies(skill_dir, portable_style="package")
    for script in sorted((skill_dir / "scripts").rglob("*.py")):
        rel = script.relative_to(skill_dir).as_posix()
        patched = patch_script_imports(script.read_text(encoding="utf-8"), rel, report)
        zipf.writestr(f"{arc_prefix}{rel}", patched)
    # Recompute requirements after generated facades/adapters are available.
    for content in portable_runtime_files(report).values():
        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue
        for root in _import_roots(tree):
            if _is_third_party(root, {RUNTIME_DIR, "_portable_runtime", "backend"}) and root not in report.third_party_imports:
                report.third_party_imports.append(root)
                req, resolved = _requirement_for_import(root)
                if req not in report.third_party_requirements:
                    report.third_party_requirements.append(req)
                if not resolved:
                    report.unresolved_requirements.append(root)
    runtime_files = portable_runtime_files(report)
    for arc, content in runtime_files.items():
        zipf.writestr(f"{arc_prefix}{arc}", content)
    zipf.writestr(f"{arc_prefix}requirements-portable.txt", "\n".join(report.third_party_requirements) + ("\n" if report.third_party_requirements else ""))
    zipf.writestr(f"{arc_prefix}.env.example", env_example(report))
    zipf.writestr(f"{arc_prefix}skill-portability.json", json.dumps(report.manifest(), ensure_ascii=False, indent=2) + "\n")
    return report


def add_inline_portable_files_to_zip(zipf: ZipFile, skill_dir: Path, arc_prefix: str = "") -> PortableReport:
    report = collect_portable_dependencies(skill_dir)
    report.portable_style = "inline"
    rendered_scripts: list[tuple[str, str]] = []
    for script in sorted((skill_dir / "scripts").rglob("*.py")):
        rel = script.relative_to(skill_dir).as_posix()
        source = script.read_text(encoding="utf-8")
        inlined, changed = _inline_script_source(source, rel, report)
        rendered_scripts.append((rel, inlined))
    if report.fallback_to_package:
        package_report = collect_portable_dependencies(skill_dir, portable_style="package")
        package_report.portable_style = "mixed"
        package_report.fallback_to_package = True
        package_report.fallback_reasons = report.fallback_reasons
        package_report.fallback_tools = sorted(set(report.fallback_tools))
        package_report.fallback_modules = sorted(set(report.fallback_modules))
        for script in sorted((skill_dir / "scripts").rglob("*.py")):
            rel = script.relative_to(skill_dir).as_posix()
            patched = patch_script_imports(script.read_text(encoding="utf-8"), rel, package_report)
            zipf.writestr(f"{arc_prefix}{rel}", patched)
        for content in portable_runtime_files(package_report).values():
            try:
                tree = ast.parse(content)
            except SyntaxError:
                continue
            for root in _import_roots(tree):
                if _is_third_party(root, {RUNTIME_DIR, "_portable_runtime", "backend"}) and root not in package_report.third_party_imports:
                    package_report.third_party_imports.append(root)
                    req, resolved = _requirement_for_import(root)
                    if req not in package_report.third_party_requirements:
                        package_report.third_party_requirements.append(req)
                    if not resolved:
                        package_report.unresolved_requirements.append(root)
        for arc, content in portable_runtime_files(package_report).items():
            zipf.writestr(f"{arc_prefix}{arc}", content)
        zipf.writestr(f"{arc_prefix}requirements-portable.txt", "\n".join(package_report.third_party_requirements) + ("\n" if package_report.third_party_requirements else ""))
        zipf.writestr(f"{arc_prefix}.env.example", env_example(package_report))
        zipf.writestr(f"{arc_prefix}skill-portability.json", json.dumps(package_report.manifest(), ensure_ascii=False, indent=2) + "\n")
        return package_report
    for rel, inlined in rendered_scripts:
        zipf.writestr(f"{arc_prefix}{rel}", inlined)
    zipf.writestr(f"{arc_prefix}requirements-portable.txt", "\n".join(report.third_party_requirements) + ("\n" if report.third_party_requirements else ""))
    zipf.writestr(f"{arc_prefix}.env.example", env_example(report))
    zipf.writestr(f"{arc_prefix}skill-portability.json", json.dumps(report.manifest(), ensure_ascii=False, indent=2) + "\n")
    return report


def add_portable_files_to_zip(
    zipf: ZipFile,
    skill_dir: Path,
    arc_prefix: str = "",
    portable_style: Literal["inline", "package"] = "inline",
) -> PortableReport:
    if portable_style == "package":
        report = add_package_portable_files_to_zip(zipf, skill_dir, arc_prefix=arc_prefix)
        report.portable_style = "package"
        return report
    if portable_style != "inline":
        raise ValueError("portable_style must be 'inline' or 'package'")
    return add_inline_portable_files_to_zip(zipf, skill_dir, arc_prefix=arc_prefix)
