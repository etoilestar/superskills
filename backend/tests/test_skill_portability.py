import io
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
    assert "from _portable_runtime.runtime_tools import strict_json_argv_guard" in patched
    assert "except ImportError:" in patched
    assert "_portable_runtime/runtime_tools.py" in names
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
    assert "_portable_runtime/skill_runtime.py" in names
    assert "LLM_API_BASE=" in env and "LLM_API_KEY=" in env and "LLM_MODEL=" in env
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
