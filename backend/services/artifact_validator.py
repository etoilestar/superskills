"""Runtime validation for JSON-declared script output artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

_ARTIFACT_FIELD_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "pdf_path": (".pdf",),
    "docx_path": (".docx",),
    "pptx_path": (".pptx",),
    "html_path": (".html", ".htm"),
    "image_path": (".png", ".jpg", ".jpeg", ".gif", ".webp"),
}
_ARTIFACT_KEYS = ("image_path", "image_paths", "pdf_path", "docx_path", "pptx_path", "html_path", "file_paths", "file_outputs")
_ARTIFACT_SUFFIXES = (".pdf", ".docx", ".pptx", ".html", ".htm", ".png", ".jpg", ".jpeg", ".gif", ".webp")
_PATH_PREFIXES = ("outputs/",)


class FileOutputValidationError(ValueError):
    """Raised when stdout JSON declares an artifact that was not really created."""

    code = "file_output_missing"


def parse_stdout_json(stdout: str) -> dict[str, Any] | None:
    stripped = (stdout or "").strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _looks_like_artifact_path(value: str) -> bool:
    normalized = value.strip().replace("\\", "/")
    if not normalized or normalized.startswith(("http://", "https://", "data:")):
        return False
    suffix = Path(normalized).suffix.lower()
    return normalized.startswith(_PATH_PREFIXES) or suffix in _ARTIFACT_SUFFIXES


def _walk_artifact_values(value: Any, *, field: str) -> list[tuple[str, str]]:
    declared: list[tuple[str, str]] = []
    if isinstance(value, str) and (field in _ARTIFACT_KEYS or _looks_like_artifact_path(value)):
        declared.append((field, value.strip()))
    elif isinstance(value, list):
        for item in value:
            declared.extend(_walk_artifact_values(item, field=field))
    elif isinstance(value, dict):
        for child_key, child_value in value.items():
            declared.extend(_walk_artifact_values(child_value, field=str(child_key)))
    return declared


def declared_artifact_paths(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Return all file-looking stdout values, independent of field names.

    Field names are metadata only.  A business Skill may call its output
    ``cover``, ``report``, ``poster_files`` or anything else; if the value looks
    like a skill-local artifact path, runtime validation checks the file exists.
    """
    declared: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for key, value in payload.items():
        for field, raw_path in _walk_artifact_values(value, field=str(key)):
            marker = (field, raw_path)
            if marker in seen:
                continue
            seen.add(marker)
            declared.append(marker)
    return declared


def stdout_declares_artifacts(stdout: str) -> bool:
    payload = parse_stdout_json(stdout)
    return bool(payload and declared_artifact_paths(payload))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_declared_artifact_path(raw_path: str, *, skill_dir: Path, cwd: Path | None = None) -> Path:
    raw_path = raw_path.strip()
    if not raw_path:
        raise FileOutputValidationError("file_output_missing: 声明的输出路径为空")
    if "\x00" in raw_path:
        raise FileOutputValidationError(f"file_output_missing: 输出路径包含非法字符: {raw_path!r}")
    path = Path(raw_path)
    if path.is_absolute():
        candidate = path.resolve()
    else:
        normalized = raw_path.replace("\\", "/").lstrip("./")
        root = skill_dir.resolve()
        if normalized.startswith("outputs/"):
            candidate = (root / path).resolve()
        else:
            candidate = ((cwd or skill_dir).resolve() / path).resolve()
    if not _is_within(candidate, skill_dir.resolve()):
        raise FileOutputValidationError(f"file_output_missing: 输出路径越界或不在安全目录内: {raw_path}")
    outputs_root = (skill_dir.resolve() / "outputs").resolve()
    if not _is_within(candidate, outputs_root):
        raise FileOutputValidationError(f"file_output_missing: 输出路径必须位于 OUTPUT_DIR/outputs 下，不能使用 assets/scripts 或其他目录: {raw_path}")
    return candidate


def _expected_extensions(field: str, raw_path: str) -> tuple[str, ...]:
    if field in _ARTIFACT_FIELD_EXTENSIONS:
        return _ARTIFACT_FIELD_EXTENSIONS[field]
    suffix = Path(raw_path).suffix.lower()
    if suffix in {".pdf", ".docx", ".pptx", ".html", ".htm"}:
        return (suffix,)
    return ()


def _validate_file_type(path: Path, extensions: tuple[str, ...]) -> None:
    if extensions and path.suffix.lower() not in extensions:
        raise FileOutputValidationError(
            f"file_output_missing: 输出文件扩展名不匹配: {path.name}，期望 {', '.join(extensions)}"
        )
    data = path.read_bytes()
    if not data:
        raise FileOutputValidationError(f"file_output_missing: 输出文件为空: {path}")
    suffix = path.suffix.lower()
    if suffix == ".pdf" and (not data.startswith(b"%PDF-") or b"%%EOF" not in data[-4096:]):
        raise FileOutputValidationError(f"file_output_missing: PDF 文件格式不合法: {path}")
    if suffix in {".docx", ".pptx"}:
        try:
            with ZipFile(path) as zf:
                names = set(zf.namelist())
        except (BadZipFile, OSError) as exc:
            raise FileOutputValidationError(f"file_output_missing: {suffix} 文件不是合法 zip 文档: {path}") from exc
        required = "word/document.xml" if suffix == ".docx" else "ppt/presentation.xml"
        if required not in names:
            raise FileOutputValidationError(f"file_output_missing: {suffix} 文件缺少 {required}: {path}")
    if suffix in {".html", ".htm"}:
        text = data.decode("utf-8", errors="replace").lower()
        if "<html" not in text and "<!doctype html" not in text:
            raise FileOutputValidationError(f"file_output_missing: HTML 文件缺少 html/doctype 标记: {path}")


def validate_stdout_file_outputs(stdout: str, *, skill_dir: Path, cwd: Path | None = None) -> list[dict[str, str]]:
    """Validate declared pdf/docx/pptx/html/file_paths in stdout JSON.

    Returns normalized ``[{path, url?}]``-ready relative path records.  Raises
    FileOutputValidationError with code ``file_output_missing`` if any declared
    artifact is missing, empty, wrong extension/type, or outside ``skill_dir``.
    Non-JSON stdout, or JSON without artifact fields, returns an empty list.
    """
    payload = parse_stdout_json(stdout)
    if not payload:
        return []
    declared = declared_artifact_paths(payload)
    if not declared:
        return []
    root = skill_dir.resolve()
    output_files: list[dict[str, str]] = []
    seen: set[str] = set()
    for field, raw_path in declared:
        path = resolve_declared_artifact_path(raw_path, skill_dir=root, cwd=cwd)
        extensions = _expected_extensions(field, raw_path)
        if not path.is_file():
            raise FileOutputValidationError(f"file_output_missing: 声明的输出文件不存在: {raw_path}")
        _validate_file_type(path, extensions)
        rel = path.relative_to(root).as_posix()
        if rel not in seen:
            seen.add(rel)
            output_files.append({"path": rel})
    return output_files
