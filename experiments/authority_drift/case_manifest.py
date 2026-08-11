"""Deterministic implementation-state manifests for captured Skills."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_file_manifest(skill_directory: Path) -> dict[str, object]:
    """Describe every regular file below ``skill_directory`` in path order."""
    root = skill_directory.resolve(strict=True)
    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ValueError(f"symbolic links are not allowed in a captured skill: {path.relative_to(root)}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"non-regular file is not allowed in a captured skill: {path.relative_to(root)}")
        relative = path.relative_to(root).as_posix()
        files.append({"path": relative, "size_bytes": path.stat().st_size, "sha256": _sha256(path)})
    canonical = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"files": files, "manifest_hash": hashlib.sha256(canonical).hexdigest()}
