import json

import pytest

from backend.services.creator import api, generation, repair


@pytest.mark.asyncio
async def test_semantic_decomposer_receives_confirmed_context_only(monkeypatch):
    captured = {}

    async def fake_complete(messages, _role, fallback_model):
        captured["payload"] = json.loads(messages[1]["content"])
        return json.dumps({"tasks": [{
            "goal": "transform source collection",
            "must_do": ["process the complete relevant collection"],
            "semantic_inputs": ["source records"],
            "semantic_outputs": ["transformed results"],
            "constraints": [],
        }]})

    monkeypatch.setattr(api, "complete_creator_role_once", fake_complete)
    request = api.PreparePlanRequest(
        user_request=("process a collection of source records, transform every relevant "
                      "record, aggregate the transformed results, and produce a final deliverable"),
        conversation_history=[{"role": "assistant", "content": "Which records?"},
                              {"role": "user", "content": "All relevant records."}],
        human_feedback="The final deliverable is confirmed.",
    )

    plan = await api._decompose_semantic_tasks(request=request, planner_model="test")

    assert plan["tasks"][0]["task_id"] == "T1"
    assert set(captured["payload"]) == {"confirmed_user_context"}
    serialized = json.dumps(captured["payload"])
    for forbidden in ("scripts/", "FunctionItems", "Blueprint", "ToolPool", "Graph",
                      "references", "assets", "capabilities"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_frozen_task_allocation_preserves_identity_and_transports_obligations(monkeypatch):
    tasks = {"tasks": [
        {"task_id": "T1", "goal": "transform source collection",
         "must_do": ["process the complete relevant collection",
                     "produce corresponding transformed results"],
         "semantic_inputs": ["source collection"],
         "semantic_outputs": ["transformed results"], "constraints": ["preserve correspondence"]},
        {"task_id": "T2", "goal": "assemble the final deliverable",
         "must_do": ["consume the transformed results", "create the requested final deliverable"],
         "semantic_inputs": ["transformed results"],
         "semantic_outputs": ["final deliverable"], "constraints": []},
    ]}
    items = [
        {"target_file": "scripts/worker_a.py", "purpose": "Transform records.",
         "inputs": ["records"], "outputs": ["transformed"], "must_do": ["existing"]},
        {"target_file": "scripts/worker_b.py", "purpose": "Assemble output.",
         "inputs": ["transformed"], "outputs": ["deliverable"], "must_do": []},
    ]

    async def fake_complete(*_args, **_kwargs):
        return json.dumps({"requirement_allocations": [
            {"requirement_id": "T1", "requirement": tasks["tasks"][0]["goal"],
             "owners": ["scripts/worker_a.py"],
             "evidence": {"responsibility": "owns transformation", "outputs": [], "capabilities": []}},
            {"requirement_id": "T2", "requirement": tasks["tasks"][1]["goal"],
             "owners": ["scripts/worker_b.py"],
             "evidence": {"responsibility": "owns assembly", "outputs": [], "capabilities": []}},
        ]})

    monkeypatch.setattr(api, "complete_creator_role_once", fake_complete)
    projection = await api._allocate_frozen_semantic_tasks(
        semantic_task_plan=tasks, function_items=items, planner_model="test")
    transported = api._transport_semantic_task_responsibilities(
        function_items=items, semantic_task_plan=tasks,
        requirement_allocations=projection["requirement_allocations"])

    assert [row["requirement_id"] for row in projection["requirement_allocations"]] == ["T1", "T2"]
    assert transported[0]["must_do"] == ["existing", *tasks["tasks"][0]["must_do"]]
    assert not set(tasks["tasks"][0]["must_do"]) & set(transported[1]["must_do"])
    assert transported[0]["constraints"][0]["source"] == "semantic_task_plan"


def test_responsibility_authority_is_shared_by_producer_and_reviewer():
    producer_source = open(generation.__file__, encoding="utf-8").read()
    reviewer_source = open(repair.__file__, encoding="utf-8").read()
    assert "EXECUTABLE RESPONSIBILITY AUTHORITY" in producer_source
    assert "FunctionItem.must_do" in producer_source
    assert "RESPONSIBILITY COMPLETENESS" in reviewer_source
    assert "FunctionItem.must_do" in reviewer_source


def test_purpose_shortening_declares_responsibility_preservation():
    source = open(api.__file__, encoding="utf-8").read()
    assert "PURPOSE IS A SUMMARY, NOT RESPONSIBILITY AUTHORITY" in source
    assert "must survive purpose shortening unchanged" in source
