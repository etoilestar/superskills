from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from experiments.authority_drift.authority_snapshot import snapshot_skill_plan
from experiments.authority_drift.case_capture import capture_clean_case


COMMIT = "d2b42b36d448bf5dac05700eefc22a022eda5c3e"


def plan(inputs: list[str] | None = None) -> dict:
    return {
        "skill_name": "demo-skill",
        "files": [{"path": "SKILL.md", "role": "skill_overview", "purpose": "instructions"}],
        "function_items": [{
            "target_file": "scripts/run.py", "role": "transform", "purpose": "transform data",
            "inputs": inputs or ["source"], "outputs": ["result"], "required_capabilities": [],
            "constraints": [],
        }],
        "responsibility_edges": [{"purpose": "provenance only"}],
    }


@pytest.fixture
def skill(tmp_path: Path) -> Path:
    root = tmp_path / "source-skill"
    (root / "scripts").mkdir(parents=True)
    (root / "assets").mkdir()
    (root / "SKILL.md").write_bytes(b"# Demo\r\nDo not normalize me.\r\n")
    (root / "scripts" / "run.py").write_bytes(b"print('ok')\n")
    (root / "assets" / "sample.bin").write_bytes(bytes(range(256)))
    return root


def capture(tmp_path: Path, skill: Path, case_id: str = "S001", structured_plan=None, evidence=None) -> Path:
    return capture_clean_case(
        cases_directory=tmp_path / "cases", case_id=case_id, user_request="Build a demo",
        structured_plan=structured_plan or plan(), skill_directory=skill,
        e2e_evidence=evidence or {"stdout": "ok", "trace": ["passed"]},
        system_commit=COMMIT, e2e_passed=True,
    )


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_successful_clean_capture(tmp_path: Path, skill: Path) -> None:
    case = capture(tmp_path, skill)
    expected = {"metadata.json", "user_request.txt", "structured_plan.json", "authority_snapshot.json",
                "e2e_evidence.json", "file_manifest.json", "skill"}
    assert {item.name for item in case.iterdir()} == expected
    assert (case / "skill/scripts/run.py").is_file()


def test_authority_snapshot_reuses_snapshot_function(tmp_path: Path, skill: Path) -> None:
    value = plan()
    case = capture(tmp_path, skill, structured_plan=value)
    assert load(case / "authority_snapshot.json") == snapshot_skill_plan(value, COMMIT)


def test_implementation_bytes_are_preserved(tmp_path: Path, skill: Path) -> None:
    case = capture(tmp_path, skill)
    for relative in ("SKILL.md", "scripts/run.py", "assets/sample.bin"):
        assert digest(skill / relative) == digest(case / "skill" / relative)


def test_manifest_is_deterministic(tmp_path: Path, skill: Path) -> None:
    first = capture(tmp_path, skill, "S001")
    second = capture(tmp_path, skill, "S002")
    assert load(first / "file_manifest.json")["manifest_hash"] == load(second / "file_manifest.json")["manifest_hash"]
    assert [item["path"] for item in load(first / "file_manifest.json")["files"]] == sorted(
        item["path"] for item in load(first / "file_manifest.json")["files"]
    )


def test_implementation_change_affects_manifest_only(tmp_path: Path, skill: Path) -> None:
    first = capture(tmp_path, skill, "S001")
    (skill / "SKILL.md").write_bytes(b"changed")
    second = capture(tmp_path, skill, "S002")
    assert load(first / "file_manifest.json")["manifest_hash"] != load(second / "file_manifest.json")["manifest_hash"]
    assert load(first / "authority_snapshot.json")["snapshot_hash"] == load(second / "authority_snapshot.json")["snapshot_hash"]


def test_authority_change_affects_authority_hash_only(tmp_path: Path, skill: Path) -> None:
    first = capture(tmp_path, skill, "S001", plan(["source"]))
    second = capture(tmp_path, skill, "S002", plan(["source", "format"]))
    assert load(first / "authority_snapshot.json")["snapshot_hash"] != load(second / "authority_snapshot.json")["snapshot_hash"]
    assert load(first / "file_manifest.json")["manifest_hash"] == load(second / "file_manifest.json")["manifest_hash"]


def test_failed_e2e_is_rejected(tmp_path: Path, skill: Path) -> None:
    with pytest.raises(ValueError, match="e2e_passed=True"):
        capture_clean_case(cases_directory=tmp_path / "cases", case_id="S001", user_request="x",
                           structured_plan=plan(), skill_directory=skill, e2e_evidence={},
                           system_commit=COMMIT, e2e_passed=False)
    assert not (tmp_path / "cases/S001").exists()


def test_existing_case_is_not_overwritten(tmp_path: Path, skill: Path) -> None:
    case = capture(tmp_path, skill)
    before = (case / "metadata.json").read_bytes()
    with pytest.raises(FileExistsError):
        capture(tmp_path, skill)
    assert (case / "metadata.json").read_bytes() == before


def test_inputs_are_not_modified(tmp_path: Path, skill: Path) -> None:
    structured, evidence = plan(), {"argv": {"nested": [1]}, "stdout": "ok"}
    structured_before, evidence_before = copy.deepcopy(structured), copy.deepcopy(evidence)
    source_before = {path.relative_to(skill): path.read_bytes() for path in skill.rglob("*") if path.is_file()}
    capture(tmp_path, skill, structured_plan=structured, evidence=evidence)
    assert structured == structured_before
    assert evidence == evidence_before
    assert source_before == {path.relative_to(skill): path.read_bytes() for path in skill.rglob("*") if path.is_file()}


def test_symlink_is_rejected_without_partial_case(tmp_path: Path, skill: Path) -> None:
    (skill / "escape").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symbolic links"):
        capture(tmp_path, skill)
    assert not (tmp_path / "cases/S001").exists()
    assert not (tmp_path / "cases/.tmp-S001").exists()


def test_case_id_path_traversal_is_rejected(tmp_path: Path, skill: Path) -> None:
    with pytest.raises(ValueError, match="path traversal"):
        capture(tmp_path, skill, "../escape")
