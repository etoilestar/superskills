"""Portable Skill ZIP dependency collection and runtime bundling."""

from __future__ import annotations

import ast
import importlib.metadata
import json
import os
import sys
import sysconfig
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import ZipFile

PORTABILITY_VERSION = "1.0"
RUNTIME_DIR = "_portable_runtime"
SYSTEM_RUNTIME_MODULES = {
    "backend.services.runtime_tools",
    "backend.services.skill_runtime",
}
DEFAULT_ENV_VARS = [
    "OUTPUT_DIR",
    "INPUT_DIR",
    "LLM_API_BASE",
    "LLM_API_KEY",
    "LLM_MODEL",
    "IMAGE_API_BASE",
    "IMAGE_API_KEY",
]
_ENV_ALIASES = {
    "LLM_BASE_URL": "LLM_API_BASE",
    "TEXT_MODEL": "LLM_MODEL",
    "IMAGE_MODEL": "IMAGE_MODEL",
}


@dataclass
class PortableReport:
    system_tools_used: list[str] = field(default_factory=list)
    copied_runtime_files: list[str] = field(default_factory=list)
    third_party_imports: list[str] = field(default_factory=list)
    third_party_requirements: list[str] = field(default_factory=list)
    unresolved_requirements: list[str] = field(default_factory=list)
    env_vars_used: list[str] = field(default_factory=list)
    patched_scripts: list[str] = field(default_factory=list)
    patched_imports: list[dict[str, str]] = field(default_factory=list)
    unsupported_tools: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def manifest(self) -> dict[str, Any]:
        return {
            "portability_version": PORTABILITY_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "system_tools_used": self.system_tools_used,
            "patched_imports": self.patched_imports,
            "requirements_file": "requirements-portable.txt",
            "env_example_file": ".env.example",
            "runtime_bundle_dir": RUNTIME_DIR,
            "unsupported_tools": self.unsupported_tools,
            "warnings": self.warnings,
            "dependency_report": {
                "copied_runtime_files": self.copied_runtime_files,
                "third_party_imports": self.third_party_imports,
                "unresolved_requirements": self.unresolved_requirements,
                "env_vars_used": self.env_vars_used,
                "patched_scripts": self.patched_scripts,
            },
        }


def _stdlib_roots() -> set[str]:
    roots = set(getattr(sys, "stdlib_module_names", set()))
    roots.update({"__future__", "typing"})
    return roots


def _local_module_roots(skill_dir: Path) -> set[str]:
    roots = {RUNTIME_DIR}
    for path in (skill_dir / "scripts").rglob("*.py"):
        roots.add(path.stem)
        try:
            rel = path.relative_to(skill_dir / "scripts").with_suffix("")
            if rel.parts:
                roots.add(rel.parts[0])
        except ValueError:
            pass
    return roots


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
            is_getenv = isinstance(func, ast.Attribute) and func.attr in {"get", "getenv"}
            if is_getenv and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                found.add(_ENV_ALIASES.get(node.args[0].value, node.args[0].value))
    return found


def _is_third_party(root: str, local_roots: set[str]) -> bool:
    if not root or root in local_roots or root == "backend":
        return False
    if root in _stdlib_roots():
        return False
    spec = sysconfig.get_paths().get("stdlib", "")
    return root not in {Path(spec).name}


def _requirement_for_import(root: str) -> tuple[str, bool]:
    try:
        mapping = importlib.metadata.packages_distributions()
        dists = mapping.get(root) or []
        if dists:
            dist = dists[0]
            return f"{dist}=={importlib.metadata.version(dist)}", True
    except Exception:
        pass
    return root, False


def collect_portable_dependencies(skill_dir: str | Path) -> PortableReport:
    skill_dir = Path(skill_dir)
    report = PortableReport()
    local_roots = _local_module_roots(skill_dir)
    third_party: set[str] = set()
    envs = set(DEFAULT_ENV_VARS)
    used_runtime_tools: set[str] = set()
    used_skill_runtime = False

    for script in sorted((skill_dir / "scripts").rglob("*.py")):
        try:
            tree = ast.parse(script.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            report.warnings.append(f"Could not parse {script.relative_to(skill_dir)}: {exc}")
            continue
        envs.update(_env_vars(tree))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module in {"backend.services.runtime_tools", "backend.services.runtime_tools.__init__"}:
                    used_runtime_tools.update(alias.name for alias in node.names if alias.name != "*")
                elif node.module.startswith("backend.services.runtime_tools."):
                    used_runtime_tools.update(alias.name for alias in node.names if alias.name != "*")
                elif node.module == "backend.services.skill_runtime":
                    used_skill_runtime = True
                    used_runtime_tools.update(alias.name for alias in node.names if alias.name != "*")
        for root in _import_roots(tree):
            if _is_third_party(root, local_roots):
                third_party.add(root)

    if used_runtime_tools:
        report.system_tools_used.append("backend.services.runtime_tools: " + ", ".join(sorted(used_runtime_tools)))
        report.copied_runtime_files.append(f"{RUNTIME_DIR}/runtime_tools.py")
    if used_skill_runtime:
        report.system_tools_used.append("backend.services.skill_runtime")
        report.copied_runtime_files.append(f"{RUNTIME_DIR}/skill_runtime.py")
        envs.update({"LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL", "IMAGE_API_BASE", "IMAGE_API_KEY"})
    report.copied_runtime_files.extend([f"{RUNTIME_DIR}/__init__.py", f"{RUNTIME_DIR}/env.py", f"{RUNTIME_DIR}/bootstrap.py"])
    report.third_party_imports = sorted(third_party)
    for root in report.third_party_imports:
        req, resolved = _requirement_for_import(root)
        report.third_party_requirements.append(req)
        if not resolved:
            report.unresolved_requirements.append(root)
    report.env_vars_used = sorted(envs)
    supported = {"strict_json_argv_guard"}
    report.unsupported_tools = sorted(t for t in used_runtime_tools if t not in supported and not t.startswith("generate_"))
    return report


def patch_script_imports(source: str, rel_path: str, report: PortableReport) -> str:
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
            target = "_portable_runtime.runtime_tools"
        elif node.module == "backend.services.skill_runtime":
            target = "_portable_runtime.skill_runtime"
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


def portable_runtime_files(report: PortableReport) -> dict[str, str]:
    unsupported = sorted(set(report.unsupported_tools))
    runtime_tools = f'''\
"""Portable shims for exported Skill runtime_tools."""
from __future__ import annotations
import json, os, sys
from pathlib import Path

UNSUPPORTED_TOOLS = {unsupported!r}

def _unsupported(name, *env):
    needed = ", ".join(env) if env else "a host adapter/environment variable"
    raise RuntimeError(f"Portable runtime tool {{name}} requires {{needed}}; configure the host adapter instead of importing backend.services.*")

def strict_json_argv_guard():
    if len(sys.argv) < 2:
        return {{}}
    try:
        return json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Expected first argv value to be strict JSON: {{exc}}") from exc

def __getattr__(name):
    if name in UNSUPPORTED_TOOLS:
        def _missing(*args, **kwargs):
            return _unsupported(name, "LLM_API_BASE", "IMAGE_API_BASE", "OUTPUT_DIR")
        return _missing
    raise AttributeError(name)
'''
    skill_runtime = '''\
"""Portable shims for exported Skill skill_runtime helpers."""
from __future__ import annotations
import base64, json, os, urllib.request
from pathlib import Path


def _env(*names):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def _required(*names):
    value = _env(*names)
    if not value:
        raise RuntimeError("Portable Skill runtime requires one of: " + ", ".join(names))
    return value


def _post_json(url, payload, api_key):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type":"application/json", "Authorization": f"Bearer {api_key}"}, method="POST")
    with urllib.request.urlopen(req, timeout=float(os.environ.get("LLM_TIMEOUT_SECONDS", "600"))) as resp:
        return json.loads(resp.read().decode())


def generate_text_with_llm(prompt, **kwargs):
    base = _required("LLM_API_BASE", "LLM_BASE_URL").rstrip("/")
    model = kwargs.get("model") or _required("LLM_MODEL", "TEXT_MODEL")
    url = base if base.endswith("/chat/completions") else base + "/v1/chat/completions"
    data = _post_json(url, {"model": model, "messages": [{"role":"user", "content": str(prompt)}], "stream": False}, _required("LLM_API_KEY", "OPENAI_API_KEY"))
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")


def generate_image(prompt, **kwargs):
    raise RuntimeError("Portable image generation requires a host adapter configured with IMAGE_API_BASE and IMAGE_API_KEY")


def output_path(name):
    out = Path(os.environ.get("OUTPUT_DIR", "./outputs")); out.mkdir(parents=True, exist_ok=True); return out / name
'''
    env_py = """from __future__ import annotations\nimport os\ndef get(name, default=''):\n    return os.environ.get(name, default)\n"""
    bootstrap = """from __future__ import annotations\nimport sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n"""
    return {
        f"{RUNTIME_DIR}/__init__.py": "\"\"\"Portable runtime bundled with an exported Skill.\"\"\"\n",
        f"{RUNTIME_DIR}/runtime_tools.py": runtime_tools,
        f"{RUNTIME_DIR}/skill_runtime.py": skill_runtime,
        f"{RUNTIME_DIR}/env.py": env_py,
        f"{RUNTIME_DIR}/bootstrap.py": bootstrap,
    }


def env_example(report: PortableReport) -> str:
    defaults = {"OUTPUT_DIR": "./outputs", "INPUT_DIR": "./inputs"}
    return "".join(f"{name}={defaults.get(name, '')}\n" for name in report.env_vars_used)


def add_portable_files_to_zip(zipf: ZipFile, skill_dir: Path, arc_prefix: str = "") -> PortableReport:
    report = collect_portable_dependencies(skill_dir)
    for script in sorted((skill_dir / "scripts").rglob("*.py")):
        rel = script.relative_to(skill_dir).as_posix()
        patched = patch_script_imports(script.read_text(encoding="utf-8"), rel, report)
        zipf.writestr(f"{arc_prefix}{rel}", patched)
    for arc, content in portable_runtime_files(report).items():
        zipf.writestr(f"{arc_prefix}{arc}", content)
    zipf.writestr(f"{arc_prefix}requirements-portable.txt", "\n".join(report.third_party_requirements) + ("\n" if report.third_party_requirements else ""))
    zipf.writestr(f"{arc_prefix}.env.example", env_example(report))
    zipf.writestr(f"{arc_prefix}skill-portability.json", json.dumps(report.manifest(), ensure_ascii=False, indent=2) + "\n")
    return report
