"""Read-only, sidecar capture of real E2E-passing Skills for experiments."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path, PurePath
from typing import Any

from .case_manifest import build_file_manifest
from .system_adapter import snapshot_skill_plan


CASE_SCHEMA_VERSION = "1.0"


class CleanCaseCollector:
    """Collector configured with the root that owns canonical case directories."""

    def __init__(self, cases_directory: str | Path) -> None:
        self.cases_directory = Path(cases_directory)

    def capture(
        self,
        *,
        case_id: str,
        user_request: str,
        structured_plan: Any,
        skill_directory: str | Path,
        e2e_evidence: Any,
        system_commit: str,
        e2e_passed: bool,
    ) -> Path:
        return capture_clean_case(
            cases_directory=self.cases_directory,
            case_id=case_id,
            user_request=user_request,
            structured_plan=structured_plan,
            skill_directory=skill_directory,
            e2e_evidence=e2e_evidence,
            system_commit=system_commit,
            e2e_passed=e2e_passed,
        )


def _json_value(value: Any) -> Any:
    """Detach supported structured representations into JSON-compatible values."""
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump())
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Case provenance must be JSON-compatible, got {type(value).__name__}")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _validate_case_id(case_id: str) -> None:
    if not isinstance(case_id, str) or not case_id or case_id in {".", ".."}:
        raise ValueError("case_id must be a non-empty path component")
    if PurePath(case_id).name != case_id or "/" in case_id or "\\" in case_id:
        raise ValueError("case_id must not contain path traversal or separators")


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
    """Atomically capture the three distinct experimental layers of a clean case."""
    if e2e_passed is not True:
        raise ValueError("Only an explicit E2E PASS may be captured as a clean case")
    _validate_case_id(case_id)
    if not isinstance(user_request, str):
        raise TypeError("user_request must be a string")

    source = Path(skill_directory)
    if not source.is_dir() or source.is_symlink():
        raise ValueError("skill_directory must be a real directory, not a symbolic link")

    # Materialize detached provenance before writing anything. Snapshot authority
    # exclusively through the Experiment 0 adapter, never from implementation.
    plan_value = _json_value(structured_plan)
    evidence_value = _json_value(e2e_evidence)
    authority_snapshot = snapshot_skill_plan(structured_plan, system_commit=system_commit)
    skill_name = authority_snapshot["skill_name"]

    cases = Path(cases_directory)
    destination = cases / case_id
    temporary = cases / f".tmp-{case_id}"
    cases.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Case already exists: {destination}")
    if temporary.exists():
        raise FileExistsError(f"Temporary capture path already exists: {temporary}")

    try:
        temporary.mkdir()
        # copytree(..., symlinks=False) would follow links. Validate first so no
        # link, including one escaping the source tree, can enter the capture.
        build_file_manifest(source)
        shutil.copytree(source, temporary / "skill", copy_function=shutil.copyfile)
        manifest = build_file_manifest(temporary / "skill")

        _write_json(temporary / "structured_plan.json", plan_value)
        _write_json(temporary / "authority_snapshot.json", authority_snapshot)
        _write_json(temporary / "e2e_evidence.json", evidence_value)
        _write_json(temporary / "file_manifest.json", manifest)
        (temporary / "user_request.txt").write_text(user_request, encoding="utf-8")
        _write_json(temporary / "metadata.json", {
            "case_schema_version": CASE_SCHEMA_VERSION,
            "case_id": case_id,
            "system_commit": system_commit,
            "skill_name": skill_name,
            "source_kind": "real_e2e_pass",
            "e2e_passed": True,
            "authority_snapshot_hash": authority_snapshot["snapshot_hash"],
            "skill_manifest_hash": manifest["manifest_hash"],
        })
        temporary.rename(destination)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return destination


# A short noun-style alias keeps the collector convenient for experiment scripts.
collect_clean_case = capture_clean_case
