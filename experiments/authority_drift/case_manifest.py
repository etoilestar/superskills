"""Deterministic manifests for captured, repairable implementation files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_file_manifest(skill_directory: Path) -> dict[str, Any]:
    """Describe every regular file below *skill_directory* without interpreting it."""
    root = Path(skill_directory)
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"Symbolic links are not allowed in captured skills: {relative}")
        if path.is_file():
            content = path.read_bytes()
            files.append({
                "path": relative,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            })
        elif not path.is_dir():
            raise ValueError(f"Unsupported skill directory entry: {relative}")

    return {
        "files": files,
        "manifest_hash": hashlib.sha256(_canonical_json(files).encode("utf-8")).hexdigest(),
    }
