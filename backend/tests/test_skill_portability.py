import io
import json
import runpy
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest


def _skill(tmp_path: Path, name="portable-skill") -> Path:
    d = tmp_path / name
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: portable\n---\n", encoding="utf-8")
    return d


def test_portable_zip_patches_runtime_tools_without_touching_source(tmp_path):
    from backend.services.skill_portability import add_portable_files_to_zip

    d = _skill(tmp_path)
    script = d / "scripts" / "run.py"
    original = "from backend.services.runtime_tools import strict_json_argv_guard\nimport json\n"
    script.write_text(original, encoding="utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        add_portable_files_to_zip(zf, d)
    assert script.read_text(encoding="utf-8") == original
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        names = set(zf.namelist())
        patched = zf.read("scripts/run.py").decode()
    assert "backend.services.runtime_tools" not in patched
    assert "_portable_runtime" not in patched
    assert "def strict_json_argv_guard" in patched
    assert "_TYPE_ALIASES" in patched
    assert not any(name.startswith("_portable_runtime/") for name in names)
    assert "skill-portability.json" in names


def test_portable_zip_skill_runtime_env_and_no_secret(tmp_path, monkeypatch):
    from backend.services.skill_portability import add_portable_files_to_zip

    monkeypatch.setenv("LLM_API_KEY", "real-secret")
    d = _skill(tmp_path)
    (d / "scripts" / "llm.py").write_text("from backend.services.skill_runtime import generate_text_with_llm\n", encoding="utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        add_portable_files_to_zip(zf, d)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        names = set(zf.namelist())
        env = zf.read(".env.example").decode()
        blob = b"".join(zf.read(n) for n in names)
    assert not any(name.startswith("_portable_runtime/") for name in names)
    assert "LLM_API_BASE=<FILL_ME>" in env and "LLM_API_KEY=<FILL_ME>" in env and "LLM_MODEL=<FILL_ME>" in env
    assert b"real-secret" not in blob


def test_import_zip_allows_portable_files_and_rejects_env(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"; skills_path.mkdir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", "---\nname: pzip\ndescription: x\n---\n")
        zf.writestr("_portable_runtime/runtime_tools.py", "x=1\n")
        zf.writestr("requirements-portable.txt", "requests\n")
        zf.writestr(".env.example", "LLM_API_KEY=\n")
        zf.writestr("skill-portability.json", '{"portability_version":"1.0"}')
        zf.writestr(".env", "SECRET=bad")
        zf.writestr("_portable_runtime/__pycache__/x.pyc", b"bad")
    with patch.object(skill_manager.settings, "skills_path", skills_path):
        result = skill_manager.import_skill_zip(buf.getvalue())
    root = skills_path / "pzip"
    assert (root / "_portable_runtime" / "runtime_tools.py").exists()
    assert (root / "requirements-portable.txt").exists()
    assert (root / ".env.example").exists()
    assert (root / "skill-portability.json").exists()
    assert not (root / ".env").exists()
    assert "portability" in result["installation"]["details"]


def test_requirements_include_third_party_not_stdlib(tmp_path):
    from backend.services.skill_portability import add_portable_files_to_zip

    d = _skill(tmp_path)
    (d / "scripts" / "deps.py").write_text("import json\nimport pytest\n", encoding="utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        add_portable_files_to_zip(zf, d)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        req = zf.read("requirements-portable.txt").decode()
    assert "pytest" in req
    assert "json" not in req


def test_strict_json_argv_guard_portable_matches_real_backend(tmp_path, monkeypatch):
    from backend.services.runtime_tools.argv_tools import strict_json_argv_guard as real_guard
    from backend.services.skill_portability import add_portable_files_to_zip

    d = _skill(tmp_path)
    (d / "scripts" / "guard.py").write_text(
        "from backend.services.runtime_tools import strict_json_argv_guard\n", encoding="utf-8"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        add_portable_files_to_zip(zf, d)
    extract_dir = tmp_path / "portable"
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        zf.extractall(extract_dir)
    namespace = runpy.run_path(str(extract_dir / "scripts" / "guard.py"))
    portable_guard = namespace["strict_json_argv_guard"]

    payload = {"name": "Ada", "count": 2}
    spec = {"name": {"type": "string", "required": True}, "count": {"type": "int", "default": 1}}
    assert portable_guard(payload, spec) == real_guard(payload, spec)
    for bad_payload in [{"name": "", "extra": 1}, {"count": "wrong"}, []]:
        real_error = None
        portable_error = None
        try:
            real_guard(bad_payload, spec)
        except Exception as exc:  # noqa: BLE001 - compare public behavior
            real_error = type(exc), str(exc)
        try:
            portable_guard(bad_payload, spec)
        except Exception as exc:  # noqa: BLE001 - compare public behavior
            portable_error = type(exc), str(exc)
        assert portable_error == real_error


def test_runtime_tool_dependency_closure_and_no_backend_copy(tmp_path, monkeypatch):
    from backend.services.skill_portability import add_portable_files_to_zip

    d = _skill(tmp_path)
    original = "from backend.services.runtime_tools import create_pdf_document\n"
    (d / "scripts" / "pdf.py").write_text(original, encoding="utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        add_portable_files_to_zip(zf, d)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        names = set(zf.namelist())
        patched = zf.read("scripts/pdf.py").decode()
        manifest = zf.read("skill-portability.json").decode()
        requirements = zf.read("requirements-portable.txt").decode()
    assert not any(name.startswith("_portable_runtime/") for name in names)
    assert "backend.services.runtime_tools" not in patched
    assert "def create_pdf_document" in patched
    assert "def _output_path" in patched
    assert not any("creator" in name or "routers" in name or "governance" in name for name in names)
    assert "backend/services/runtime_tools/document_tools.py" not in names
    assert "copied_modules" in manifest
    assert "reportlab" in requirements
    assert (d / "scripts" / "pdf.py").read_text(encoding="utf-8") == original


def test_runtime_closure_requirements_and_env_placeholders(tmp_path, monkeypatch):
    from backend.services.skill_portability import add_portable_files_to_zip

    monkeypatch.delenv("SEARCHXNG_BASE_URL", raising=False)
    d = _skill(tmp_path)
    (d / "scripts" / "search.py").write_text(
        "from backend.services.runtime_tools import web_search\n", encoding="utf-8"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        add_portable_files_to_zip(zf, d)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        requirements = zf.read("requirements-portable.txt").decode()
        env = zf.read(".env.example").decode()
        manifest = json.loads(zf.read("skill-portability.json").decode())
    assert "SEARCHXNG_BASE_URL=<FILL_ME>" in env
    assert manifest["env_placeholders"]["SEARCHXNG_BASE_URL"] == "<FILL_ME>"
    assert "web_search" in manifest["adapted_tools"]
    assert isinstance(requirements, str)


def test_skill_runtime_adapter_signature_accepts_real_call_and_errors_at_runtime(tmp_path, monkeypatch):
    from backend.services.skill_portability import add_portable_files_to_zip

    monkeypatch.delenv("LLM_API_BASE", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    d = _skill(tmp_path)
    (d / "scripts" / "llm.py").write_text(
        "from backend.services.skill_runtime import generate_text_with_llm\n", encoding="utf-8"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        add_portable_files_to_zip(zf, d)
    extract_dir = tmp_path / "llm_portable"
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        zf.extractall(extract_dir)
        manifest = json.loads((extract_dir / "skill-portability.json").read_text())
    namespace = runpy.run_path(str(extract_dir / "scripts" / "llm.py"))
    generate_text_with_llm = namespace["generate_text_with_llm"]

    assert "generate_text_with_llm" in manifest["adapted_tools"]
    assert not (extract_dir / "_portable_runtime").exists()
    with pytest.raises(RuntimeError, match="LLM_BASE_URL|LLM_API_BASE|fill .env.example"):
        generate_text_with_llm("hello", system="sys", temperature=0.1)


def test_inline_cross_module_class_constant_and_name_conflict(tmp_path, monkeypatch):
    import backend.services.skill_portability as portability

    runtime = tmp_path / "runtime_tools"
    runtime.mkdir()
    (runtime / "__init__.py").write_text("from .helper_tools import public_tool\n", encoding="utf-8")
    (runtime / "other.py").write_text("def other_helper(value):\n    return value + 1\n\nUNUSED = 'not copied'\n", encoding="utf-8")
    (runtime / "helper_tools.py").write_text(
        "from .other import other_helper\n\nCONST_VALUE = 3\n\nclass Box:\n    def __init__(self, value):\n        self.value = value\n\ndef _output_path(value):\n    return other_helper(value) + CONST_VALUE\n\ndef public_tool(value):\n    return Box(_output_path(value)).value\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(portability, "RUNTIME_TOOLS_DIR", runtime)
    d = _skill(tmp_path / "skill")
    original = "from backend.services.runtime_tools import public_tool\n\ndef _output_path(value):\n    return 'user'\n\nRESULT = public_tool(1)\n"
    (d / "scripts" / "main.py").write_text(original, encoding="utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        portability.add_portable_files_to_zip(zf, d)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        names = set(zf.namelist())
        script = zf.read("scripts/main.py").decode()
    assert not any(name.startswith("_portable_runtime/") for name in names)
    assert "def public_tool" in script
    assert "class Box" in script
    assert "CONST_VALUE = 3" in script
    assert "def other_helper" in script
    assert "def _portable_output_path" in script
    assert "def _output_path(value):\n    return 'user'" in script
    namespace = runpy.run_path(str(_write_script(tmp_path, script)))
    assert namespace["RESULT"] == 5
    assert (d / "scripts" / "main.py").read_text(encoding="utf-8") == original


def _write_script(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "rendered.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_inline_fallback_to_package_for_dynamic_runtime_module(tmp_path, monkeypatch):
    import backend.services.skill_portability as portability

    runtime = tmp_path / "runtime_tools"
    runtime.mkdir()
    (runtime / "__init__.py").write_text("from .dynamic_tools import dynamic_tool\n", encoding="utf-8")
    (runtime / "dynamic_tools.py").write_text("def dynamic_tool(name):\n    return eval(name)\n", encoding="utf-8")
    monkeypatch.setattr(portability, "RUNTIME_TOOLS_DIR", runtime)
    d = _skill(tmp_path / "fallback")
    (d / "scripts" / "dyn.py").write_text("from backend.services.runtime_tools import dynamic_tool\n", encoding="utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        portability.add_portable_files_to_zip(zf, d)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("skill-portability.json").decode())
    assert any(name.startswith("_portable_runtime/") for name in names)
    assert manifest["fallback_to_package"] is True
    assert manifest["portable_style"] == "mixed"
    assert manifest["fallback_reasons"]


def test_explicit_package_style_keeps_portable_runtime(tmp_path):
    from backend.services.skill_portability import add_portable_files_to_zip

    d = _skill(tmp_path)
    (d / "scripts" / "run.py").write_text("from backend.services.runtime_tools import strict_json_argv_guard\n", encoding="utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        add_portable_files_to_zip(zf, d, portable_style="package")
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("skill-portability.json").decode())
    assert "_portable_runtime/backend/services/runtime_tools/argv_tools.py" in names
    assert manifest["portable_style"] == "package"
