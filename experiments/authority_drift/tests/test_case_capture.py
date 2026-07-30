from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from backend.services.skill_plan import SkillPlan, SkillPlanEntry
from experiments.authority_drift.case_capture import capture_clean_case
from experiments.authority_drift.system_adapter import snapshot_skill_plan


COMMIT = "d2b42b36d448bf5dac05700eefc22a022eda5c3e"


def plan() -> SkillPlan:
    return SkillPlan(
        skill_name="clean-skill",
        files=[SkillPlanEntry(
            path="SKILL.md", file_type="skill_md", file_kind="skill_doc",
            role="skill_overview", purpose="document the skill",
        )],
        function_items=[{
            "target_file": "scripts/run.py", "role": "runner", "purpose": "run",
            "inputs": ["source"], "outputs": ["artifact"], "default_values": {},
            "required_capabilities": ["read"], "constraints": [],
        }],
    )


@pytest.fixture
def skill(tmp_path: Path) -> Path:
    root = tmp_path / "generated-skill"
    (root / "scripts").mkdir(parents=True)
    (root / "assets").mkdir()
    (root / "SKILL.md").write_text("# Exact markdown\n", encoding="utf-8")
    (root / "scripts" / "run.py").write_bytes(b"print('exact')\n")
    (root / "assets" / "sample.bin").write_bytes(bytes(range(256)))
    return root


def capture(cases: Path, case_id: str, skill: Path, value=None, **kwargs) -> Path:
    return capture_clean_case(
        cases_directory=cases, case_id=case_id, user_request="请生成技能",
        structured_plan=value or plan(), skill_directory=skill,
        e2e_evidence=kwargs.pop("e2e_evidence", {"stdout": "PASS", "trace": [1]}),
        system_commit=COMMIT, e2e_passed=kwargs.pop("e2e_passed", True), **kwargs,
    )


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_successful_clean_capture(tmp_path: Path, skill: Path):
    case = capture(tmp_path / "cases", "S001", skill)
    expected = {
        "metadata.json", "user_request.txt", "structured_plan.json",
        "authority_snapshot.json", "e2e_evidence.json", "file_manifest.json", "skill",
    }
    assert {item.name for item in case.iterdir()} == expected
    assert (case / "skill/scripts/run.py").exists()
    assert load(case / "metadata.json")["source_kind"] == "real_e2e_pass"


def test_authority_snapshot_reuses_experiment_zero(tmp_path: Path, skill: Path):
    value = plan()
    case = capture(tmp_path / "cases", "S001", skill, value)
    assert load(case / "authority_snapshot.json") == snapshot_skill_plan(value, system_commit=COMMIT)


def test_implementation_bytes_are_preserved(tmp_path: Path, skill: Path):
    case = capture(tmp_path / "cases", "S001", skill)
    for source in (path for path in skill.rglob("*") if path.is_file()):
        copied = case / "skill" / source.relative_to(skill)
        assert hashlib.sha256(copied.read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest()


def test_manifest_is_deterministic(tmp_path: Path, skill: Path):
    first = capture(tmp_path / "cases", "S001", skill)
    second = capture(tmp_path / "cases", "S002", skill)
    assert load(first / "file_manifest.json")["manifest_hash"] == load(second / "file_manifest.json")["manifest_hash"]
    assert [item["path"] for item in load(first / "file_manifest.json")["files"]] == [
        "SKILL.md", "assets/sample.bin", "scripts/run.py",
    ]


def test_implementation_change_affects_manifest_only(tmp_path: Path, skill: Path):
    first = capture(tmp_path / "cases", "S001", skill)
    (skill / "SKILL.md").write_text("changed", encoding="utf-8")
    second = capture(tmp_path / "cases", "S002", skill)
    assert load(first / "file_manifest.json")["manifest_hash"] != load(second / "file_manifest.json")["manifest_hash"]
    assert load(first / "metadata.json")["authority_snapshot_hash"] == load(second / "metadata.json")["authority_snapshot_hash"]


def test_authority_change_affects_authority_hash_only(tmp_path: Path, skill: Path):
    original = plan()
    changed_item = {**original.function_items[0], "inputs": ["different"]}
    changed = replace(original, function_items=[changed_item])
    first = capture(tmp_path / "cases", "S001", skill, original)
    second = capture(tmp_path / "cases", "S002", skill, changed)
    assert load(first / "metadata.json")["authority_snapshot_hash"] != load(second / "metadata.json")["authority_snapshot_hash"]
    assert load(first / "metadata.json")["skill_manifest_hash"] == load(second / "metadata.json")["skill_manifest_hash"]


def test_failed_e2e_is_rejected_without_partial_case(tmp_path: Path, skill: Path):
    with pytest.raises(ValueError, match="E2E PASS"):
        capture(tmp_path / "cases", "S001", skill, e2e_passed=False)
    assert not (tmp_path / "cases/S001").exists()


def test_existing_case_cannot_be_overwritten(tmp_path: Path, skill: Path):
    case = capture(tmp_path / "cases", "S001", skill)
    before = (case / "metadata.json").read_bytes()
    with pytest.raises(FileExistsError):
        capture(tmp_path / "cases", "S001", skill)
    assert (case / "metadata.json").read_bytes() == before


def test_inputs_are_not_modified(tmp_path: Path, skill: Path):
    value = plan()
    evidence = {"stdout": ["unchanged"], "artifact": {"ok": True}}
    value_before, evidence_before = copy.deepcopy(value), copy.deepcopy(evidence)
    bytes_before = {path.relative_to(skill): path.read_bytes() for path in skill.rglob("*") if path.is_file()}
    capture(tmp_path / "cases", "S001", skill, value, e2e_evidence=evidence)
    assert value == value_before
    assert evidence == evidence_before
    assert bytes_before == {path.relative_to(skill): path.read_bytes() for path in skill.rglob("*") if path.is_file()}


def test_symlink_and_traversal_are_rejected(tmp_path: Path, skill: Path):
    (skill / "escape").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="Symbolic links"):
        capture(tmp_path / "cases", "S001", skill)
    with pytest.raises(ValueError, match="traversal"):
        capture(tmp_path / "cases", "../S002", skill)
