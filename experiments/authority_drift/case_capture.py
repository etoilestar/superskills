"""Read-only, sidecar collector for clean E2E PASS experiment cases."""

from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .authority_snapshot import snapshot_skill_plan
from .case_manifest import build_file_manifest
from .system_adapter import to_plain_data


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _validate_case_id(case_id: str) -> None:
    if not isinstance(case_id, str) or not case_id or case_id in {".", ".."}:
        raise ValueError("case_id must be a non-empty path component")
    if Path(case_id).name != case_id or "/" in case_id or "\\" in case_id:
        raise ValueError("case_id must not contain path traversal or separators")


def _copy_skill(source: Path, destination: Path) -> None:
    root = source.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("skill_directory must be a directory")
    # Reject all links, including links that happen to remain inside the tree: a
    # capture must have an unambiguous set of ordinary source files.
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"symbolic links are not allowed in a captured skill: {path.relative_to(root)}")
    shutil.copytree(root, destination, copy_function=shutil.copyfile)


def capture_clean_case(
    *,
    cases_directory: str | Path,
    case_id: str,
    user_request: str,
    structured_plan: Any,
    skill_directory: str | Path,
    e2e_evidence: Any,
    system_commit: str,
    e2e_passed: bool,
) -> Path:
    """Atomically capture an explicitly confirmed real E2E PASS Skill."""
    if e2e_passed is not True:
        raise ValueError("a clean case requires explicit e2e_passed=True")
    _validate_case_id(case_id)
    if not isinstance(user_request, str):
        raise TypeError("user_request must be a string")

    plan = copy.deepcopy(to_plain_data(structured_plan))
    evidence = copy.deepcopy(to_plain_data(e2e_evidence))
    snapshot = snapshot_skill_plan(plan, system_commit)
    cases_root = Path(cases_directory)
    cases_root.mkdir(parents=True, exist_ok=True)
    destination = cases_root / case_id
    temporary = cases_root / f".tmp-{case_id}"
    if destination.exists():
        raise FileExistsError(f"case already exists: {destination}")
    if temporary.exists():
        raise FileExistsError(f"temporary capture already exists: {temporary}")

    try:
        temporary.mkdir()
        _copy_skill(Path(skill_directory), temporary / "skill")
        manifest = build_file_manifest(temporary / "skill")
        _write_json(temporary / "structured_plan.json", plan)
        _write_json(temporary / "authority_snapshot.json", snapshot)
        _write_json(temporary / "e2e_evidence.json", evidence)
        _write_json(temporary / "file_manifest.json", manifest)
        (temporary / "user_request.txt").write_text(user_request, encoding="utf-8")
        metadata = {
            "case_schema_version": "1.0",
            "case_id": case_id,
            "system_commit": system_commit,
            "skill_name": snapshot["skill_name"],
            "source_kind": "real_e2e_pass",
            "e2e_passed": True,
            "authority_snapshot_hash": snapshot["snapshot_hash"],
            "skill_manifest_hash": manifest["manifest_hash"],
        }
        _write_json(temporary / "metadata.json", metadata)
        os.rename(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


# A concise alias for callers that already operate in the clean-case domain.
capture_case = capture_clean_case
