"""Skill Executor — runs kernel/scripts actions requested by the LLM.

Supported actions: init, write, write_file, validate, package, run_script.
All return a uniform dict: {action, name, success, message, path}.
run_script additionally returns: {stdout, stderr, exit_code, filename}.
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

from ..config import PROJECT_ROOT, settings

logger = logging.getLogger(__name__)

_KERNEL_SCRIPT_TIMEOUT = 60


def _run_kernel_script(script_filename: str, args: list[str]) -> subprocess.CompletedProcess:
    script_path = settings.kernel_path / "scripts" / script_filename
    if not script_path.is_file():
        raise FileNotFoundError(f"Kernel 脚本不存在: {script_path}")

    return subprocess.run(
        [sys.executable, str(script_path), *args],
        capture_output=True,
        text=True,
        timeout=_KERNEL_SCRIPT_TIMEOUT,
    )


def run_action(action: dict) -> dict:
    """Execute a single skill file-system action.

    Args:
        action: dict with at least {"action": str, "name": str} and optional fields.

    Returns:
        {"action": str, "name": str, "success": bool, "message": str, "path": str | None}
    """
    action_type = action.get("action", "")
    name = action.get("name", "").strip()

    if not name:
        return {
            "action": action_type,
            "name": name,
            "success": False,
            "message": "缺少 name 参数",
            "path": None,
        }

    skill_dir = settings.skills_path / name

    try:
        if action_type == "init":
            return _run_init(name, skill_dir)
        if action_type == "write":
            return _run_write(name, action.get("content", ""), skill_dir)
        if action_type == "write_file":
            return _run_write_file(
                name,
                action.get("folder", ""),
                action.get("filename", ""),
                action.get("content", ""),
                skill_dir,
            )
        if action_type == "validate":
            return _run_validate(name, skill_dir)
        if action_type == "package":
            return _run_package(name, skill_dir)
        if action_type == "run_script":
            return _run_script(
                name,
                action.get("filename", ""),
                action.get("args", []),
                action.get("stdin", ""),
                skill_dir,
            )
        return {
            "action": action_type,
            "name": name,
            "success": False,
            "message": f"未知动作类型: {action_type}",
            "path": None,
        }
    except Exception as exc:  # pragma: no cover
        logger.exception("skill_executor error for action %r", action_type)
        return {
            "action": action_type,
            "name": name,
            "success": False,
            "message": "操作执行失败，请重试",
            "path": None,
        }


# ---------------------------------------------------------------------------
# Action implementations
# ---------------------------------------------------------------------------

def _run_init(name: str, skill_dir: Path) -> dict:
    if skill_dir.exists():
        return {
            "action": "init",
            "name": name,
            "success": True,
            "message": f"目录已存在，跳过初始化: {skill_dir.name}",
            "path": str(skill_dir),
        }
    try:
        result = _run_kernel_script(
            "init_skill.py",
            [name, "--path", str(settings.skills_path)],
        )
    except Exception as exc:
        return {
            "action": "init",
            "name": name,
            "success": False,
            "message": f"初始化失败：{exc}",
            "path": None,
        }
    if result.returncode != 0:
        return {
            "action": "init",
            "name": name,
            "success": False,
            "message": (result.stderr or result.stdout or "初始化失败，请检查 skill 名称是否合法").strip(),
            "path": None,
        }
    
    # 删除示例文件，只保留空目录结构
    for example_file in [
        skill_dir / "scripts" / "example.py",
        skill_dir / "references" / "api_reference.md",
        skill_dir / "assets" / "example_asset.txt",
    ]:
        if example_file.exists():
            example_file.unlink()
    
    return {
        "action": "init",
        "name": name,
        "success": True,
        "message": f"已创建 {name} 目录结构",
        "path": str(skill_dir),
    }


def _run_write(name: str, content: str, skill_dir: Path) -> dict:
    if not content:
        return {
            "action": "write",
            "name": name,
            "success": False,
            "message": "缺少 content 参数",
            "path": None,
        }
    from . import skill_manager  # local import to avoid circular deps

    skill_manager.save_skill(name, content)
    skill_md_path = skill_dir / "SKILL.md"
    return {
        "action": "write",
        "name": name,
        "success": True,
        "message": f"SKILL.md 已写入",
        "path": str(skill_md_path),
    }


def _run_validate(name: str, skill_dir: Path) -> dict:
    try:
        result = _run_kernel_script("quick_validate.py", [str(skill_dir)])
    except Exception as exc:
        return {
            "action": "validate",
            "name": name,
            "success": False,
            "message": f"校验失败：{exc}",
            "path": None,
        }

    valid = result.returncode == 0
    message = (result.stdout or result.stderr or "").strip() or "校验失败"
    return {
        "action": "validate",
        "name": name,
        "success": valid,
        "message": message,
        "path": str(skill_dir / "SKILL.md") if valid else None,
    }


def _run_package(name: str, skill_dir: Path) -> dict:
    output_dir = skill_dir / "dist"
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = _run_kernel_script(
            "package_skill.py",
            [str(skill_dir), str(output_dir)],
        )
    except Exception as exc:
        return {
            "action": "package",
            "name": name,
            "success": False,
            "message": f"打包失败：{exc}",
            "path": None,
        }

    if result.returncode != 0:
        return {
            "action": "package",
            "name": name,
            "success": False,
            "message": (
                result.stderr
                or result.stdout
                or "打包失败，请先执行 validate 确认 SKILL.md 格式正确"
            ).strip(),
            "path": None,
        }
    return {
        "action": "package",
        "name": name,
        "success": True,
        "message": f"已打包为 {name}.skill",
        "path": str(output_dir / f"{name}.skill"),
    }


# ---------------------------------------------------------------------------
# Allowed folders for write_file
# ---------------------------------------------------------------------------

_ALLOWED_WRITE_FOLDERS = {"scripts", "references", "assets"}


def _safe_filename(filename: str) -> str | None:
    """Return the base filename if it is safe, otherwise None."""
    safe = Path(filename).name
    if (
        not safe
        or safe.startswith(".")
        or "\x00" in safe
        or len(safe) > 255
    ):
        return None
    return safe


def _run_write_file(name: str, folder: str, filename: str, content: str, skill_dir: Path) -> dict:
    if folder not in _ALLOWED_WRITE_FOLDERS:
        return {
            "action": "write_file",
            "name": name,
            "success": False,
            "message": f"folder 必须是以下之一: {sorted(_ALLOWED_WRITE_FOLDERS)}",
            "path": None,
        }
    safe = _safe_filename(filename)
    if safe is None:
        return {
            "action": "write_file",
            "name": name,
            "success": False,
            "message": "文件名非法（不允许路径分隔符或隐藏文件）",
            "path": None,
        }
    if not content:
        return {
            "action": "write_file",
            "name": name,
            "success": False,
            "message": "缺少 content 参数",
            "path": None,
        }
    if not skill_dir.exists():
        return {
            "action": "write_file",
            "name": name,
            "success": False,
            "message": f"Skill '{name}' 目录不存在，请先执行 init",
            "path": None,
        }
    target_dir = skill_dir / folder
    target_dir.mkdir(exist_ok=True)
    dest = target_dir / safe
    dest.write_text(content, encoding="utf-8")
    return {
        "action": "write_file",
        "name": name,
        "success": True,
        "message": f"{folder}/{safe} 已写入",
        "path": str(dest),
    }


# ---------------------------------------------------------------------------
# run_script action
# ---------------------------------------------------------------------------

_SCRIPT_RUN_TIMEOUT = 30   # seconds
_MAX_OUTPUT_BYTES = 100 * 1024  # 100 KB per stream

_SNAPSHOT_EXCLUDE_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", "dist"}


def _snapshot_skill_files(skill_dir: Path) -> set[str]:
    """Return a set of relative POSIX paths for all files under *skill_dir*.

    Excludes common non-output directories to keep the snapshot lightweight.
    """
    result: set[str] = set()
    if not skill_dir.exists():
        return result
    for f in skill_dir.rglob("*"):
        if not f.is_file():
            continue
        try:
            rel = f.relative_to(skill_dir)
        except ValueError:
            continue
        if any(part in _SNAPSHOT_EXCLUDE_DIRS for part in rel.parts):
            continue
        result.add(rel.as_posix())
    return result


def _build_script_runtime_env(skill_dir: Path) -> dict[str, str]:
    """Return stable environment variables injected into generated scripts.

    The creator prompts generated scripts to use these names instead of
    hard-coded model IDs or service URLs.  Building this map in one place keeps
    sandbox script runs and creator validation trial runs consistent.
    """
    env = {**os.environ}
    env.update({
        "LLM_BASE_URL": settings.llm_base_url,
        "IMAGE_BASE_URL": settings.image_base_url,
        "IMAGE_API_KEY": settings.image_api_key or env.get("IMAGE_API_KEY") or env.get("LLM_API_KEY") or env.get("OPENAI_API_KEY") or "ollama",
        "DEFAULT_MODEL": settings.default_model,
        "TEXT_MODEL": settings.text_model or settings.default_model,
        "CODE_MODEL": settings.code_model or settings.default_model,
        "IMAGE_MODEL": settings.image_model or settings.default_model,
        "VISION_MODEL": settings.vision_model or settings.default_model,
        "IMAGE_SIZE": settings.image_size,
        "OUTPUT_DIR": str(skill_dir / "outputs"),
        "INPUT_DIR": str(skill_dir / "inputs"),
        "PYTHONPATH": os.pathsep.join(
            part for part in [str(PROJECT_ROOT), env.get("PYTHONPATH", "")] if part
        ),
    })
    api_key = settings.llm_api_key or settings.openai_api_key or env.get("LLM_API_KEY") or env.get("OPENAI_API_KEY") or "ollama"
    env["LLM_API_KEY"] = api_key
    env["OPENAI_API_KEY"] = settings.openai_api_key or api_key
    return env


def _run_script(name: str, filename: str, args: list, stdin: str, skill_dir: Path) -> dict:
    """Execute a Python script from skills/{name}/scripts/ and return its output.

    Called via asyncio.to_thread so blocking subprocess.run is safe here.
    """
    _empty = {"stdout": "", "stderr": "", "exit_code": -1, "filename": filename}

    safe = _safe_filename(filename)
    if safe is None or not safe.endswith(".py"):
        return {
            "action": "run_script", "name": name, "success": False,
            "message": "文件名非法或不是 .py 文件", "path": None, **_empty,
        }

    if not skill_dir.exists():
        return {
            "action": "run_script", "name": name, "success": False,
            "message": f"Skill '{name}' 目录不存在，请先执行 init", "path": None, **_empty,
        }

    script_path = skill_dir / "scripts" / safe
    if not script_path.is_file():
        return {
            "action": "run_script", "name": name, "success": False,
            "message": f"脚本 '{safe}' 不存在，请先用 write_file 写入", "path": None, **_empty,
        }

    for arg in (args or []):
        if "\x00" in str(arg):
            return {
                "action": "run_script", "name": name, "success": False,
                "message": "参数包含非法字符", "path": None, **_empty,
            }

    # Snapshot the skill directory before execution to detect new output files.
    pre_snapshot = _snapshot_skill_files(skill_dir)

    try:
        proc = subprocess.run(
            [sys.executable, str(script_path), *(str(a) for a in (args or []))],
            input=stdin.encode("utf-8") if stdin else b"",
            capture_output=True,
            timeout=_SCRIPT_RUN_TIMEOUT,
            cwd=str(skill_dir / "scripts"),
            env=_build_script_runtime_env(skill_dir),
        )
        stdout = proc.stdout[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        stderr = proc.stderr[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        success = proc.returncode == 0

        result: dict = {
            "action": "run_script",
            "name": name,
            "success": success,
            "message": f"脚本退出码: {proc.returncode}",
            "path": str(script_path),
            "filename": safe,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": proc.returncode,
        }

        # Detect newly created files and attach download metadata.
        if success:
            post_snapshot = _snapshot_skill_files(skill_dir)
            new_files = sorted(post_snapshot - pre_snapshot)
            if new_files:
                result["output_files"] = [
                    {"path": f, "url": f"/api/skills/{name}/files/{f}"}
                    for f in new_files
                ]

        return result
    except subprocess.TimeoutExpired:
        return {
            "action": "run_script", "name": name, "success": False,
            "message": f"脚本执行超时（超过 {_SCRIPT_RUN_TIMEOUT} 秒）",
            "path": None, **_empty,
        }
    except Exception as exc:  # pragma: no cover
        logger.exception("run_script subprocess error")
        return {
            "action": "run_script", "name": name, "success": False,
            "message": f"脚本执行失败: {exc}", "path": None, **_empty,
        }
