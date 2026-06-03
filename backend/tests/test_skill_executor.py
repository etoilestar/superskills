"""Tests for backend/services/skill_executor.py."""

import pytest
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _prepare_skill(skills_path: Path, name: str) -> Path:
    """Create a minimal skill directory."""
    skill_dir = skills_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\n# {name}\n"
    )
    return skill_dir


# ---------------------------------------------------------------------------
# run_action: missing name guard
# ---------------------------------------------------------------------------

def test_run_action_missing_name():
    from backend.services.skill_executor import run_action

    result = run_action({"action": "init", "name": ""})
    assert result["success"] is False
    assert "name" in result["message"]


def test_run_action_unknown_action(tmp_path):
    from backend.services import skill_executor

    skills_path = tmp_path / "skills"
    _prepare_skill(skills_path, "x")

    with patch.object(skill_executor.settings, "skills_path", skills_path):
        result = skill_executor.run_action({"action": "bogus", "name": "x"})

    assert result["success"] is False
    assert "未知" in result["message"]


# ---------------------------------------------------------------------------
# _safe_filename
# ---------------------------------------------------------------------------

def test_safe_filename_normal():
    from backend.services.skill_executor import _safe_filename

    assert _safe_filename("main.py") == "main.py"


def test_safe_filename_with_path_traversal():
    from backend.services.skill_executor import _safe_filename

    # Path.name strips the directory component
    assert _safe_filename("../evil.py") == "evil.py"


def test_safe_filename_hidden_file():
    from backend.services.skill_executor import _safe_filename

    assert _safe_filename(".hidden") is None


def test_safe_filename_null_byte():
    from backend.services.skill_executor import _safe_filename

    assert _safe_filename("bad\x00file.py") is None


def test_safe_filename_too_long():
    from backend.services.skill_executor import _safe_filename

    assert _safe_filename("a" * 300 + ".py") is None


# ---------------------------------------------------------------------------
# _run_write_file
# ---------------------------------------------------------------------------

def test_run_write_file_success(tmp_path):
    from backend.services import skill_executor

    skills_path = tmp_path / "skills"
    _prepare_skill(skills_path, "sk")

    with patch.object(skill_executor.settings, "skills_path", skills_path):
        result = skill_executor._run_write_file(
            "sk", "scripts", "main.py", "print('hi')", skills_path / "sk"
        )

    assert result["success"] is True
    assert (skills_path / "sk" / "scripts" / "main.py").read_text() == "print('hi')"


def test_run_write_file_invalid_folder(tmp_path):
    from backend.services import skill_executor

    skills_path = tmp_path / "skills"
    _prepare_skill(skills_path, "sk")

    result = skill_executor._run_write_file(
        "sk", "evil", "main.py", "code", skills_path / "sk"
    )
    assert result["success"] is False
    assert "folder" in result["message"]


def test_run_write_file_invalid_filename(tmp_path):
    from backend.services import skill_executor

    skills_path = tmp_path / "skills"
    _prepare_skill(skills_path, "sk")

    result = skill_executor._run_write_file(
        "sk", "scripts", ".hidden", "code", skills_path / "sk"
    )
    assert result["success"] is False
    assert "非法" in result["message"]


def test_run_write_file_empty_content(tmp_path):
    from backend.services import skill_executor

    skills_path = tmp_path / "skills"
    _prepare_skill(skills_path, "sk")

    result = skill_executor._run_write_file(
        "sk", "scripts", "main.py", "", skills_path / "sk"
    )
    assert result["success"] is False
    assert "content" in result["message"]


def test_run_write_file_skill_not_found(tmp_path):
    from backend.services import skill_executor

    ghost_dir = tmp_path / "ghost"
    result = skill_executor._run_write_file(
        "ghost", "scripts", "main.py", "code", ghost_dir
    )
    assert result["success"] is False
    assert "不存在" in result["message"]


# ---------------------------------------------------------------------------
# _run_script
# ---------------------------------------------------------------------------

def test_run_script_success(tmp_path):
    from backend.services import skill_executor

    skills_path = tmp_path / "skills"
    skill_dir = _prepare_skill(skills_path, "sk")
    (skill_dir / "scripts").mkdir(exist_ok=True)
    (skill_dir / "scripts" / "hello.py").write_text("print('hello from test')")

    result = skill_executor._run_script("sk", "hello.py", [], "", skill_dir)

    assert result["success"] is True
    assert "hello from test" in result["stdout"]
    assert result["exit_code"] == 0


def test_run_script_not_py_file(tmp_path):
    from backend.services import skill_executor

    skill_dir = tmp_path / "sk"
    skill_dir.mkdir()
    result = skill_executor._run_script("sk", "run.sh", [], "", skill_dir)
    assert result["success"] is False
    assert ".py" in result["message"]


def test_run_script_file_not_found(tmp_path):
    from backend.services import skill_executor

    skill_dir = tmp_path / "sk"
    skill_dir.mkdir()
    (skill_dir / "scripts").mkdir()
    result = skill_executor._run_script("sk", "missing.py", [], "", skill_dir)
    assert result["success"] is False
    assert "不存在" in result["message"]


def test_run_script_null_byte_in_arg(tmp_path):
    from backend.services import skill_executor

    skills_path = tmp_path / "skills"
    skill_dir = _prepare_skill(skills_path, "sk")
    (skill_dir / "scripts").mkdir(exist_ok=True)
    (skill_dir / "scripts" / "ok.py").write_text("pass")

    result = skill_executor._run_script("sk", "ok.py", ["bad\x00arg"], "", skill_dir)
    assert result["success"] is False
    assert "非法" in result["message"]


def test_run_script_exit_nonzero(tmp_path):
    from backend.services import skill_executor

    skill_dir = tmp_path / "sk"
    skill_dir.mkdir()
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "fail.py").write_text("import sys; sys.exit(1)")

    result = skill_executor._run_script("sk", "fail.py", [], "", skill_dir)
    assert result["success"] is False
    assert result["exit_code"] == 1


def test_run_script_detects_output_files(tmp_path):
    from backend.services import skill_executor

    skill_dir = tmp_path / "sk"
    skill_dir.mkdir()
    (skill_dir / "scripts").mkdir()
    out_dir = skill_dir / "outputs"
    out_dir.mkdir()
    script = f"with open('{out_dir / 'result.txt'}', 'w') as f: f.write('done')"
    (skill_dir / "scripts" / "gen.py").write_text(script)

    result = skill_executor._run_script("sk", "gen.py", [], "", skill_dir)
    assert result["success"] is True
    assert "output_files" in result
    paths = [f["path"] for f in result["output_files"]]
    assert any("result.txt" in p for p in paths)


# ---------------------------------------------------------------------------
# _snapshot_skill_files
# ---------------------------------------------------------------------------

def test_snapshot_excludes_pycache(tmp_path):
    from backend.services.skill_executor import _snapshot_skill_files

    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    (tmp_path / "main.py").write_text("pass")

    snapshot = _snapshot_skill_files(tmp_path)
    assert "main.py" in snapshot
    assert not any("__pycache__" in p for p in snapshot)


def test_build_script_runtime_env_injects_model_variables(tmp_path):
    from backend.services import skill_executor

    with patch.object(skill_executor.settings, "llm_base_url", "http://llm.test"), \
         patch.object(skill_executor.settings, "image_base_url", "http://image.test"), \
         patch.object(skill_executor.settings, "text_model", "text-a"), \
         patch.object(skill_executor.settings, "image_model", "image-a"), \
         patch.object(skill_executor.settings, "image_api_key", "image-key"), \
         patch.object(skill_executor.settings, "llm_api_key", "key-a"):
        env = skill_executor._build_script_runtime_env(tmp_path)

    assert env["LLM_BASE_URL"] == "http://llm.test"
    assert env["IMAGE_BASE_URL"] == "http://image.test"
    assert env["TEXT_MODEL"] == "text-a"
    assert env["IMAGE_MODEL"] == "image-a"
    assert env["IMAGE_API_KEY"] == "image-key"
    assert "PYTHONPATH" in env
    assert env["LLM_API_KEY"] == "key-a"
    assert env["OUTPUT_DIR"] == str(tmp_path / "outputs")


def test_skill_runtime_trial_image_helper_writes_output_file(monkeypatch, tmp_path):
    from backend.services.skill_runtime import generate_stable_diffusion_image

    monkeypatch.setenv("SKILL_TRIAL_RUN", "1")
    monkeypatch.setenv("IMAGE_MODEL", "stable-diffusion-2-1-base")

    result = generate_stable_diffusion_image(
        "一只猫",
        output_dir=tmp_path,
        filename_prefix="猫 图",
    )

    image_path = Path(result["image_path"])
    assert result["model"] == "stable-diffusion-2-1-base"
    assert result["source"] == "trial"
    assert image_path.is_file()
    assert image_path.parent == tmp_path
    assert image_path.suffix == ".png"
