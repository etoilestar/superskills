import asyncio
import mimetypes
import re as _re
import shutil
import subprocess
import sys as _sys
import time as _time
from pathlib import Path as _Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from ..services.skill_manager import (
    delete_asset,
    delete_skill,
    export_skill_zip,
    get_asset,
    get_execution_skill_dir,
    get_skill,
    get_skill_versions,
    get_visible_skill_dir,
    import_skill_zip,
    list_skill_assets,
    list_skills,
    rollback_skill,
    save_asset,
    save_skill,
    save_skill_zip,
    saved_skill_zip_path,
    upgrade_skill_zip,
    update_asset,
)
from ..services.skill_governance import get_allowlist, get_events, transition_skill_status, update_allowlist

router = APIRouter(prefix="/api/skills", tags=["skills"])

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
_MAX_ZIP_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
SkillMode = Literal["manage", "sandbox", "creator"]


class SaveSkillRequest(BaseModel):
    name: str
    content: str


class SkillStatusRequest(BaseModel):
    action: str
    reason: str = ""


class RollbackSkillRequest(BaseModel):
    version: str


@router.get("")
async def get_all_skills(
    mode: SkillMode = Query("manage", description="治理模式：manage / sandbox / creator"),
    include_hidden: bool = Query(False),
):
    """List all skills in the skills directory."""
    return list_skills(mode=mode, include_hidden=include_hidden)


@router.post("/import")
async def import_skill_from_zip(
    file: UploadFile = File(...),
    overwrite: bool = Form(False),
):
    """Import a skill from a .zip file (e.g. downloaded from skillsmp).

    Returns 409 Conflict when the skill already exists and overwrite is False.
    """
    if file.size is not None and file.size > _MAX_ZIP_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="ZIP 文件超过 50 MB 限制")
    data = await file.read()
    if len(data) > _MAX_ZIP_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="ZIP 文件超过 50 MB 限制")
    try:
        return import_skill_zip(data, overwrite=overwrite)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail={"message": f"Skill '{exc}' 已存在", "skill_name": str(exc)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{skill_name}/upgrade")
async def upgrade_skill_from_zip(skill_name: str, file: UploadFile = File(...)):
    if file.size is not None and file.size > _MAX_ZIP_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="ZIP 文件超过 50 MB 限制")
    data = await file.read()
    if len(data) > _MAX_ZIP_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="ZIP 文件超过 50 MB 限制")
    try:
        return upgrade_skill_zip(skill_name, data)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/governance/allowlist")
async def read_allowlist():
    return get_allowlist()


@router.put("/governance/allowlist")
async def write_allowlist(payload: dict):
    return update_allowlist(payload)


@router.get("/{skill_name}")
async def get_one_skill(skill_name: str, mode: SkillMode = Query("manage", description="治理模式：manage / sandbox / creator")):
    """Get a single skill's metadata and SKILL.md content."""
    try:
        return get_skill(skill_name, mode=mode)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post("")
async def create_or_update_skill(request: SaveSkillRequest):
    """Create or overwrite a skill with provided SKILL.md content."""
    return save_skill(request.name, request.content)


@router.delete("/{skill_name}")
async def remove_skill(skill_name: str):
    """Delete a skill directory."""
    try:
        delete_skill(skill_name)
        return {"message": f"Skill '{skill_name}' deleted"}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{skill_name}/status")
async def change_skill_status(skill_name: str, request: SkillStatusRequest):
    try:
        return transition_skill_status(skill_name, request.action, reason=request.reason)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{skill_name}/events")
async def list_skill_events(skill_name: str):
    return {"events": get_events(skill_name)}


@router.get("/{skill_name}/versions")
async def list_skill_versions(skill_name: str):
    try:
        return get_skill_versions(skill_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{skill_name}/rollback")
async def rollback_skill_version(skill_name: str, request: RollbackSkillRequest):
    try:
        return rollback_skill(skill_name, request.version)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{skill_name}/assets")
async def get_skill_assets(skill_name: str):
    """List asset files grouped by sub-directory for a skill."""
    try:
        return list_skill_assets(skill_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post("/{skill_name}/assets")
async def upload_skill_asset(
    skill_name: str,
    file: UploadFile = File(...),
    folder: str = Form("assets"),
):
    """Upload a reference file to a skill sub-directory (max 10 MB)."""
    # Reject oversized files before reading body when Content-Length is available
    if file.size is not None and file.size > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB limit")
    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB limit")
    try:
        return save_asset(skill_name, folder, file.filename or "", data)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.get("/{skill_name}/assets/{folder}/{filename}")
async def get_skill_asset_content(skill_name: str, folder: str, filename: str):
    """Read the text content of a single asset file."""
    try:
        content = get_asset(skill_name, folder, filename)
        return {"content": content}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        if "Binary" in str(exc):
            raise HTTPException(status_code=415, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))


class UpdateAssetRequest(BaseModel):
    content: str


@router.put("/{skill_name}/assets/{folder}/{filename}")
async def update_skill_asset_content(skill_name: str, folder: str, filename: str, request: UpdateAssetRequest):
    """Overwrite the text content of a single asset file."""
    try:
        return update_asset(skill_name, folder, filename, request.content)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.delete("/{skill_name}/assets/{folder}/{filename}")
async def remove_skill_asset(skill_name: str, folder: str, filename: str):
    """Delete a single asset file from a skill sub-directory."""
    try:
        delete_asset(skill_name, folder, filename)
        return {"message": f"Deleted '{filename}' from '{folder}'"}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


# ---------------------------------------------------------------------------
# Script execution endpoint
# ---------------------------------------------------------------------------

_SCRIPT_RUN_TIMEOUT = 30  # seconds


class RunScriptRequest(BaseModel):
    args: list[str] = []
    stdin: str = ""


class RunScriptResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


@router.post("/{skill_name}/scripts/{filename}/run", response_model=RunScriptResponse)
async def run_skill_script(skill_name: str, filename: str, request: RunScriptRequest):
    """Execute a Python script from a skill's scripts/ directory.

    Restricted to scripts that live under skills/{skill_name}/scripts/.
    Output is capped at 100 KB each for stdout and stderr.
    Execution is limited to 30 seconds.
    """
    from pathlib import Path as _Path

    # Validate filename (no path traversal)
    safe_name = _Path(filename).name
    if (
        not safe_name
        or safe_name.startswith(".")
        or "\x00" in safe_name
        or len(safe_name) > 255
        or not safe_name.endswith(".py")
    ):
        raise HTTPException(status_code=400, detail="文件名非法或不是 .py 文件")

    try:
        skill_dir = get_execution_skill_dir(skill_name, mode="sandbox")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    script_path = skill_dir / "scripts" / safe_name
    if not script_path.is_file():
        raise HTTPException(status_code=404, detail=f"脚本 '{safe_name}' 不存在")

    # Validate extra args: no shell injection (no shell=True, but sanitise list)
    for arg in request.args:
        if "\x00" in arg:
            raise HTTPException(status_code=400, detail="参数包含非法字符")

    try:
        proc = await asyncio.create_subprocess_exec(
            _sys.executable,
            str(script_path),
            *request.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(skill_dir / "scripts"),
        )
        _MAX_OUTPUT = 100 * 1024  # 100 KB
        stdin_bytes = request.stdin.encode("utf-8") if request.stdin else None
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes),
                timeout=_SCRIPT_RUN_TIMEOUT,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise HTTPException(status_code=408, detail=f"脚本执行超时（超过 {_SCRIPT_RUN_TIMEOUT} 秒）")

        return RunScriptResponse(
            stdout=stdout_bytes[:_MAX_OUTPUT].decode("utf-8", errors="replace"),
            stderr=stderr_bytes[:_MAX_OUTPUT].decode("utf-8", errors="replace"),
            exit_code=proc.returncode,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"脚本执行失败: {exc}") from exc


_SANDBOX_INPUT_ALLOWED_SUFFIXES: frozenset[str] = frozenset({
    ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".xml", ".html", ".htm",
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt",
    ".zip", ".tar", ".gz", ".log",
})

_SESSION_TTL_SECONDS = 24 * 3600


def _cleanup_expired_sessions(inputs_dir: _Path) -> None:
    if not inputs_dir.is_dir():
        return
    now = _time.time()
    for session_dir in list(inputs_dir.iterdir()):
        if session_dir.is_dir() and (now - session_dir.stat().st_mtime) > _SESSION_TTL_SECONDS:
            shutil.rmtree(session_dir, ignore_errors=True)


@router.post("/{skill_name}/sandbox-inputs")
async def upload_sandbox_input(
    skill_name: str,
    file: UploadFile = File(...),
    session_id: str = Form("default"),
):
    """Upload an input file for a sandbox chat session.

    Files are stored under skills/{skill_name}/inputs/{session_id}/.
    Only common data/document file types are accepted (max 10 MB).
    """
    # Validate session_id (alphanumeric, hyphens, underscores only)
    if not session_id or not _re.fullmatch(r"[a-zA-Z0-9_\-]{1,64}", session_id):
        raise HTTPException(status_code=400, detail="session_id 格式非法")

    if file.size is not None and file.size > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB limit")

    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB limit")

    safe_name = _Path(file.filename or "upload").name
    if not safe_name or safe_name.startswith(".") or "\x00" in safe_name or len(safe_name) > 255:
        raise HTTPException(status_code=400, detail="文件名非法")

    suffix = _Path(safe_name).suffix.lower()
    if suffix not in _SANDBOX_INPUT_ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail=f"不支持的文件类型 '{suffix}'，允许类型：{', '.join(sorted(_SANDBOX_INPUT_ALLOWED_SUFFIXES))}",
        )

    try:
        skill_dir = get_execution_skill_dir(skill_name, mode="sandbox").resolve()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    target_dir = skill_dir / "inputs" / session_id
    target_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_expired_sessions(skill_dir / "inputs")
    dest = target_dir / safe_name
    if dest.exists():
        stem = _Path(safe_name).stem
        suffix = _Path(safe_name).suffix
        counter = 1
        while dest.exists():
            dest = target_dir / f"{stem}_{counter}{suffix}"
            counter += 1
        safe_name = dest.name
    dest.write_bytes(data)

    rel = dest.relative_to(skill_dir)
    return {
        "path": rel.as_posix(),
        "url": f"/api/skills/{skill_name}/files/{rel.as_posix()}",
        "filename": safe_name,
        "size": len(data),
    }


@router.delete("/{skill_name}/sandbox-inputs/{session_id}")
async def cleanup_sandbox_session(skill_name: str, session_id: str):
    """Delete a sandbox session's input directory and its outputs.

    Called by the frontend when a sandbox conversation ends (page close,
    refresh, or explicit reset) to ensure session files are cleaned up
    promptly rather than waiting for the 24h TTL expiry.
    """
    if not session_id or not _re.fullmatch(r"[a-zA-Z0-9_\-]{1,64}", session_id):
        raise HTTPException(status_code=400, detail="session_id 格式非法")

    try:
        skill_dir = get_execution_skill_dir(skill_name, mode="sandbox").resolve()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    cleaned = []

    # Remove inputs/<session_id>/
    inputs_session_dir = skill_dir / "inputs" / session_id
    if inputs_session_dir.is_dir():
        shutil.rmtree(inputs_session_dir, ignore_errors=True)
        cleaned.append(f"inputs/{session_id}")

    # Remove outputs/<session_id>/ if exists (sandbox outputs may be session-scoped)
    outputs_session_dir = skill_dir / "outputs" / session_id
    if outputs_session_dir.is_dir():
        shutil.rmtree(outputs_session_dir, ignore_errors=True)
        cleaned.append(f"outputs/{session_id}")

    return {"status": "cleaned", "session_id": session_id, "cleaned": cleaned}


@router.get("/{skill_name}/outputs")
async def list_skill_outputs(skill_name: str):
    """List files generated by skill scripts in the outputs/ directory.

    Returns name, relative path, download URL, size (bytes), and last-modified
    timestamp (Unix epoch seconds) for each file.
    """
    try:
        skill_dir = get_execution_skill_dir(skill_name, mode="sandbox").resolve()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    outputs_dir = skill_dir / "outputs"
    if not outputs_dir.exists():
        return {"files": []}

    files = []
    for f in sorted(outputs_dir.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(skill_dir)
        stat = f.stat()
        files.append({
            "name": f.name,
            "path": rel.as_posix(),
            "url": f"/api/skills/{skill_name}/files/{rel.as_posix()}",
            "size": stat.st_size,
            "modified": stat.st_mtime,
        })
    return {"files": files}


@router.get("/{skill_name}/export")
async def export_skill(skill_name: str, portable: bool = Query(False), mode: SkillMode = Query("manage")):
    try:
        data = export_skill_zip(skill_name, portable=portable, mode=mode)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    filename = f"{skill_name}.portable.zip" if portable else f"{skill_name}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{skill_name}/save-zip")
async def save_skill_zip_to_server(
    skill_name: str,
    portable: bool = Query(True),
    mode: SkillMode = Query("manage"),
    output_dir: str | None = Query(None),
):
    try:
        return save_skill_zip(skill_name, portable=portable, mode=mode, output_dir=output_dir)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{skill_name}/saved-zips/{filename}")
async def download_saved_skill_zip(skill_name: str, filename: str):
    try:
        path = saved_skill_zip_path(skill_name, filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return FileResponse(path=str(path), filename=filename, media_type="application/zip")


@router.get("/{skill_name}/files/{filepath:path}")
async def download_skill_file(skill_name: str, filepath: str):
    """Download a file generated by a skill script.

    Serves any file that lives under skills/{skill_name}/, with path-traversal
    protection.  Typical use: output files written to skills/{name}/outputs/.
    """
    try:
        skill_dir = get_visible_skill_dir(skill_name, mode="manage").resolve()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    # Reject obviously malicious inputs before constructing the path
    if "\x00" in filepath:
        raise HTTPException(status_code=400, detail="非法文件路径")

    target = (skill_dir / filepath).resolve()

    # Path-traversal guard: resolved path must stay inside the skill directory
    try:
        target.relative_to(skill_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="路径越界")

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    # Infer MIME type from file extension for proper browser preview
    media_type, _ = mimetypes.guess_type(str(target))
    if media_type is None:
        media_type = "application/octet-stream"

    return FileResponse(
        path=str(target),
        filename=_Path(filepath).name,
        media_type=media_type,
    )
