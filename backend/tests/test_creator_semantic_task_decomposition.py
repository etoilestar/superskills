import copy
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
        ], "requirement_channels": {"T1": "executable", "T2": "executable"}})

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


def test_owner_change_rebuilds_responsibilities_from_clean_function_items():
    obligation = "process the complete source set"
    tasks = {"tasks": [{
        "task_id": "T1", "goal": "transform the source set",
        "must_do": [obligation], "semantic_inputs": ["source set"],
        "semantic_outputs": ["transformed set"], "constraints": [],
    }]}
    clean_items = [
        {"target_file": "scripts/worker_a.py", "purpose": "First worker.", "must_do": []},
        {"target_file": "scripts/worker_b.py", "purpose": "Second worker.", "must_do": []},
    ]
    allocation_a = [{"requirement_id": "T1", "requirement": tasks["tasks"][0]["goal"],
                     "owners": ["scripts/worker_a.py"], "evidence": {"responsibility": "", "outputs": [], "capabilities": []}}]
    allocation_b = [{"requirement_id": "T1", "requirement": tasks["tasks"][0]["goal"],
                     "owners": ["scripts/worker_b.py"], "evidence": {"responsibility": "", "outputs": [], "capabilities": []}}]

    transported_a = api._transport_semantic_task_responsibilities(
        function_items=copy.deepcopy(clean_items), semantic_task_plan=tasks,
        requirement_allocations=allocation_a)
    transported_b = api._transport_semantic_task_responsibilities(
        function_items=copy.deepcopy(clean_items), semantic_task_plan=tasks,
        requirement_allocations=allocation_b)

    assert obligation in transported_a[0]["must_do"]
    assert obligation not in transported_b[0]["must_do"]
    assert obligation in transported_b[1]["must_do"]


@pytest.mark.asyncio
async def test_frozen_task_allocation_preserves_three_channels(monkeypatch):
    tasks = {"tasks": [
        {"task_id": "T1", "goal": "transform source records", "must_do": ["transform records"],
         "semantic_inputs": [], "semantic_outputs": [], "constraints": []},
        {"task_id": "T2", "goal": "provide an authorized static guidance resource",
         "must_do": ["provide guidance"], "semantic_inputs": [], "semantic_outputs": [], "constraints": []},
        {"task_id": "T3", "goal": "return a direct final response", "must_do": ["return response"],
         "semantic_inputs": [], "semantic_outputs": [], "constraints": []},
    ]}
    items = [{"target_file": "scripts/worker.py", "purpose": "Transform records.", "must_do": []}]
    response = {
        "requirement_allocations": [
            {"requirement_id": "T1", "requirement": tasks["tasks"][0]["goal"],
             "owners": ["scripts/worker.py"], "evidence": {"responsibility": "", "outputs": [], "capabilities": []}},
            {"requirement_id": "T2", "requirement": tasks["tasks"][1]["goal"],
             "owners": [], "evidence": {"responsibility": "", "outputs": [], "capabilities": []}},
            {"requirement_id": "T3", "requirement": tasks["tasks"][2]["goal"],
             "owners": [], "evidence": {"responsibility": "", "outputs": [], "capabilities": []}},
        ],
        "requirement_channels": {"T1": "executable", "T2": "resource", "T3": "direct"},
    }

    async def fake_complete(*_args, **_kwargs):
        return json.dumps(response)

    monkeypatch.setattr(api, "complete_creator_role_once", fake_complete)
    projection = await api._allocate_frozen_semantic_tasks(
        semantic_task_plan=tasks, function_items=items, planner_model="test")
    transported = api._transport_semantic_task_responsibilities(
        function_items=items, semantic_task_plan=tasks,
        requirement_allocations=projection["requirement_allocations"])

    assert projection["requirement_channels"] == response["requirement_channels"]
    assert transported[0]["must_do"] == tasks["tasks"][0]["must_do"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [
    lambda data: data["requirement_allocations"][1].update(owners=["scripts/worker.py"]),
    lambda data: data["requirement_allocations"][2].update(owners=["scripts/worker.py"]),
    lambda data: data["requirement_allocations"][0].update(owners=[]),
    lambda data: data["requirement_channels"].update(T1="unknown"),
    lambda data: data["requirement_allocations"][0].update(owners=["scripts/missing.py"]),
    lambda data: data["requirement_allocations"].pop(0),
    lambda data: (data["requirement_allocations"].append({
        "requirement_id": "T4", "requirement": "extra", "owners": [], "evidence": {"responsibility": "", "outputs": [], "capabilities": []}}),
        data["requirement_channels"].update(T4="direct")),
    lambda data: data["requirement_allocations"][0].update(requirement="rewritten"),
])
async def test_frozen_task_allocation_rejects_invalid_protocol(monkeypatch, mutate):
    tasks = {"tasks": [
        {"task_id": "T1", "goal": "transform source records", "must_do": [],
         "semantic_inputs": [], "semantic_outputs": [], "constraints": []},
        {"task_id": "T2", "goal": "provide guidance", "must_do": [],
         "semantic_inputs": [], "semantic_outputs": [], "constraints": []},
        {"task_id": "T3", "goal": "return response", "must_do": [],
         "semantic_inputs": [], "semantic_outputs": [], "constraints": []},
    ]}
    response = {
        "requirement_allocations": [
            {"requirement_id": "T1", "requirement": "transform source records",
             "owners": ["scripts/worker.py"], "evidence": {"responsibility": "", "outputs": [], "capabilities": []}},
            {"requirement_id": "T2", "requirement": "provide guidance", "owners": [], "evidence": {"responsibility": "", "outputs": [], "capabilities": []}},
            {"requirement_id": "T3", "requirement": "return response", "owners": [], "evidence": {"responsibility": "", "outputs": [], "capabilities": []}},
        ],
        "requirement_channels": {"T1": "executable", "T2": "resource", "T3": "direct"},
    }
    mutate(response)

    async def fake_complete(*_args, **_kwargs):
        return json.dumps(response)

    monkeypatch.setattr(api, "complete_creator_role_once", fake_complete)
    with pytest.raises((api.PreparePlanProtocolError, ValueError)):
        await api._allocate_frozen_semantic_tasks(
            semantic_task_plan=tasks,
            function_items=[{"target_file": "scripts/worker.py", "purpose": "Transform."}],
            planner_model="test",
        )


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
