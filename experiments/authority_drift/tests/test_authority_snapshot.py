from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace

import pytest

from backend.services.skill_plan import SkillPlan, SkillPlanEntry
from experiments.authority_drift.authority_snapshot import snapshot_json
from experiments.authority_drift.system_adapter import snapshot_skill_plan


COMMIT = "d2b42b36d448bf5dac05700eefc22a022eda5c3e"


def plan() -> SkillPlan:
    files = [
        SkillPlanEntry(path="SKILL.md", file_type="skill_md", file_kind="skill_doc", role="skill_overview", purpose="doc"),
        SkillPlanEntry(path="scripts/run.py", file_type="script", file_kind="script", role="worker", purpose="run", asset_source="", required=True, can_skip=False),
    ]
    functions = [{
        "target_file": "scripts/run.py", "role": "worker", "purpose": "transform",
        "inputs": ["source", "options"], "outputs": ["artifact", "summary"],
        "default_values": {"options": {"format": "json", "flags": ["b", "a"]}},
        "required_capabilities": ["write_file", "read_data"],
        "constraints": [{"kind": "limit", "value": 10}],
    }]
    return SkillPlan(skill_name="实验技能", files=files, function_items=functions, responsibility_edges=[{"from": "a", "to": "b"}])


def snap(value):
    return snapshot_skill_plan(value, system_commit=COMMIT)


def test_determinism_ten_runs():
    snapshots = [snap(plan()) for _ in range(10)]
    assert len({snapshot_json(item) for item in snapshots}) == 1
    assert len({item["snapshot_hash"] for item in snapshots}) == 1


def test_input_isolation():
    value = plan()
    before = copy.deepcopy(value)
    snap(value)
    assert value == before


def test_ordering_robustness():
    original = plan()
    second_item = {**original.function_items[0], "target_file": "scripts/other.py"}
    a = replace(original, files=list(original.files), function_items=[original.function_items[0], second_item])
    b = replace(original, files=list(reversed(original.files)), function_items=[second_item, original.function_items[0]])
    assert snap(a)["snapshot_hash"] == snap(b)["snapshot_hash"]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (["parse", "summarize", "render"], ["render", "summarize", "parse"]),
        ([{"steps": ["a", "b"]}], [{"steps": ["b", "a"]}]),
    ],
)
def test_ordered_default_list_changes_hash(first, second):
    original = plan()
    item_a = {**original.function_items[0], "default_values": {"pipeline": first}}
    item_b = {**original.function_items[0], "default_values": {"pipeline": second}}
    assert snap(replace(original, function_items=[item_a]))["snapshot_hash"] != snap(
        replace(original, function_items=[item_b])
    )["snapshot_hash"]


@pytest.mark.parametrize("change", ["inputs", "outputs", "default_values", "path"])
def test_authority_sensitivity(change):
    original = plan()
    changed = copy.deepcopy(original)
    if change == "path":
        changed = replace(changed, files=[replace(changed.files[0], path="GUIDE.md"), *changed.files[1:]])
    else:
        item = dict(changed.function_items[0])
        item[change] = {"new": 1} if change == "default_values" else ["new"]
        changed = replace(changed, function_items=[item])
    assert snap(original)["snapshot_hash"] != snap(changed)["snapshot_hash"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("responsibility_edges", [{"entirely": "different"}]),
        ("SKILL.md", "changed prose"),
        ("script_source", "raise RuntimeError"),
        ("runtime_evidence", {"result": "changed"}),
        ("stdout", "changed"),
        ("argv", ["--changed"]),
        ("tool_pool", ["anything"]),
    ],
)
def test_repairable_and_observed_state_is_excluded(field, value):
    original = plan()
    as_dict = {**copy.deepcopy(original.__dict__), field: value}
    assert snap(original)["snapshot_hash"] == snap(as_dict)["snapshot_hash"]


@pytest.mark.parametrize(
    "value",
    [
        {"files": [], "function_items": []},
        {"skill_name": "x", "function_items": []},
        {"skill_name": "x", "files": []},
        {"skill_name": "x", "files": [{"file_type": "script"}], "function_items": []},
        {"skill_name": "x", "files": [], "function_items": [{"role": "worker"}]},
    ],
)
def test_malformed_plan_fails(value):
    with pytest.raises(ValueError):
        snap(value)


def test_explicit_empty_function_items_is_valid():
    snapshot = snap({"skill_name": "docs", "files": [], "function_items": []})
    assert snapshot["function_items"] == []


def test_snapshot_hash_matches_canonical_payload():
    snapshot = snap(plan())
    payload = {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(encoded.encode("utf-8")).hexdigest() == snapshot["snapshot_hash"]


def test_schema_contains_only_allowlisted_fields():
    snapshot = snap(plan())
    assert set(snapshot) == {"snapshot_version", "system_commit", "skill_name", "file_plan", "function_items", "snapshot_hash"}
    assert set(snapshot["file_plan"][0]) == {"path", "file_type", "file_kind", "asset_source", "required", "can_skip"}
    assert set(snapshot["function_items"][0]) == {"target_file", "role", "purpose", "inputs", "outputs", "default_values", "required_capabilities", "constraints"}
