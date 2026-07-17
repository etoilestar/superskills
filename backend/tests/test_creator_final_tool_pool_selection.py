"""Focused regression tests for Creator final ToolPool planning."""

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.creator import api
from backend.services.creator.tool_pool_models import ToolPoolFileBinding, ToolPoolModel, ToolPoolTool
from backend.services.creator.tool_pool_store import load_tool_pool, save_tool_pool


def test_system_image_generation_is_selectable_with_existing_function_contract():
    catalog = api._creator_tool_catalog_for_planner()
    by_id = {tool["tool_id"]: tool for tool in catalog}

    assert "system_image_generation" in by_id
    functions = by_id["system_image_generation"]["functions"]
    assert any(
        function.get("function_name") == "generate_stable_diffusion_image"
        and function.get("import_path") == "backend.services.skill_runtime"
        for function in functions
    )


def test_creator_selectable_catalog_filters_capability_shells(monkeypatch):
    callable_tool = {
        "functions": [
            {
                "function_name": "call_me",
                "import_path": "backend.services.skill_runtime",
            }
        ]
    }
    no_functions = {"functions": []}
    missing_name = {
        "functions": [
            {
                "function_name": "",
                "import_path": "backend.services.skill_runtime",
            }
        ]
    }
    missing_import_path = {
        "functions": [
            {
                "function_name": "call_me",
                "import_path": "",
            }
        ]
    }

    assert api._creator_tool_has_callable_contract(callable_tool)
    assert not api._creator_tool_has_callable_contract(no_functions)
    assert not api._creator_tool_has_callable_contract(missing_name)
    assert not api._creator_tool_has_callable_contract(missing_import_path)


def test_embedding_recall_keeps_exact_match_plus_top_k_union_contract():
    source = inspect.getsource(api._recall_creator_tool_candidates)

    assert "exact Registry capability matches" in source
    assert "UNION" in source
    assert "per-query embedding top-k" in source


@pytest.mark.asyncio
async def test_recalled_candidates_are_separated_from_available_optional_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    monkeypatch.setattr(
        api,
        "_recall_creator_tool_candidates",
        lambda file_specs, top_k: (
            [
                {"tool_id": "exact_alpha", "recalled_for_capabilities": ["exact"]},
                {"tool_id": "embedding_beta", "recalled_for_capabilities": ["embedding"]},
            ],
            "exact capability recall + embedding top-k recall union",
        ),
    )
    monkeypatch.setattr(api, "_apply_planner_tool_pool_patch", lambda **kwargs: {"patch_present": True})

    async def fail_if_called(**kwargs):
        raise AssertionError(f"unexpected LLM selector call: {kwargs.get('phase')}")

    monkeypatch.setattr(api, "_complete_creator_json_object_once", fail_if_called)

    result = await api._plan_final_tool_pool(
        skill_name="demo",
        file_specs=[{"path": "scripts/a.py", "required": True, "required_capabilities": ["exact"]}],
    )

    assert result["recalled_candidate_tool_ids"] == ["exact_alpha", "embedding_beta"]
    assert result["available_optional_tool_ids"] == []
    assert result["unavailable_tool_ids"] == ["embedding_beta", "exact_alpha"]
    assert result["tool_bindings_by_file"] == {"scripts/a.py": []}
    assert result["planner_output"]["recalled_candidate_tool_ids"] == ["exact_alpha", "embedding_beta"]
    assert result["planner_output"]["desired_tool_ids"] == []
    assert result["planner_output"]["authorized_tool_ids"] == []
    assert "decisions" not in result["planner_output"]
    assert result["planner_output"]["recall_source"] == "exact capability recall + embedding top-k recall union"
    assert result["planner_output"]["selection_mode"] == "function_item_owned_optional_recall_no_llm"


@pytest.mark.asyncio
async def test_final_tool_pool_planning_never_calls_llm_selector_phases(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    monkeypatch.setattr(api, "_recall_creator_tool_candidates", lambda file_specs, top_k: ([{"tool_id": "callable_alpha"}], "test"))
    monkeypatch.setattr(api, "_apply_planner_tool_pool_patch", lambda **kwargs: {"patch_present": True})

    async def fake_json(**kwargs):
        calls.append(kwargs.get("phase"))
        return {"decisions": {"callable_alpha": False}}

    monkeypatch.setattr(api, "_complete_creator_json_object_once", fake_json)

    result = await api._plan_final_tool_pool(skill_name="demo", file_specs=[{"path": "scripts/a.py", "required": True}])

    assert calls == []
    assert "final_tool_selection" not in calls
    assert "final_tool_selection_convergence" not in calls
    assert result["recalled_candidate_tool_ids"] == ["callable_alpha"]
    assert result["desired_tool_ids"] == []
    assert result["authorized_tool_ids"] == []


@pytest.mark.asyncio
async def test_auto_final_tool_planning_refreshes_optional_view_without_removal(monkeypatch, tmp_path):
    apply_kwargs = {}
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir(parents=True)
    save_tool_pool(
        skill_dir,
        ToolPoolModel(
            tools=[
                ToolPoolTool(tool_id="system_text_generation", status="allowed", source="system_required"),
                ToolPoolTool(tool_id="system_image_generation", status="allowed", source="system_required"),
                ToolPoolTool(tool_id="older_recalled_tool", status="allowed", source="blueprint_preselect"),
            ]
        ),
    )
    monkeypatch.setattr(api, "_recall_creator_tool_candidates", lambda file_specs, top_k: ([{"tool_id": "new_recalled_tool"}], "test"))

    def fake_apply(**kwargs):
        apply_kwargs.update(kwargs)
        return {"patch_present": True}

    monkeypatch.setattr(api, "_apply_planner_tool_pool_patch", fake_apply)

    result = await api._plan_final_tool_pool(skill_name="demo", file_specs=[{"path": "scripts/a.py", "required": True}])

    patch = result["computed_patch"]["tool_pool_patch"]
    assert patch["remove_tool_requests"] == []
    assert apply_kwargs["allow_remove"] is False
    assert result["recalled_candidate_tool_ids"] == ["new_recalled_tool"]
    assert result["tool_bindings_by_file"] == {"scripts/a.py": []}


@pytest.mark.asyncio
async def test_existing_system_generation_tools_are_not_deleted_when_not_recalled(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir(parents=True)
    save_tool_pool(
        skill_dir,
        ToolPoolModel(
            tools=[
                ToolPoolTool(tool_id="system_text_generation", status="allowed", source="system_required"),
                ToolPoolTool(tool_id="system_image_generation", status="allowed", source="system_required"),
            ]
        ),
    )
    monkeypatch.setattr(api, "_recall_creator_tool_candidates", lambda file_specs, top_k: ([], "test"))

    result = await api._plan_final_tool_pool(skill_name="demo", file_specs=[{"path": "scripts/a.py", "required": True}])

    pool = load_tool_pool(skill_dir)
    allowed = {tool.tool_id for tool in pool.tools if tool.status == "allowed"}
    assert result["computed_patch"]["tool_pool_patch"]["remove_tool_requests"] == []
    assert result["apply_result"]["removed"] == 0
    assert "system_text_generation" in allowed
    assert "system_image_generation" in allowed


def test_backend_gate_still_rejects_unusable_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    result = api._apply_planner_tool_pool_patch(
        skill_name="demo",
        planner_output={
            "tool_pool_patch": {
                "add_tool_requests": [
                    {"candidate_tool_id": "definitely_not_registered_tool"},
                    {"candidate_tool_id": "system_text_generation"},
                ],
                "remove_tool_requests": [],
            }
        },
        source_phase="final_contract_tool_planning",
        allow_remove=False,
    )

    pool = load_tool_pool(tmp_path / "demo")
    denied_ids = {item.tool_id for item in pool.denied_requests}
    allowed_ids = {tool.tool_id for tool in pool.tools if tool.status == "allowed"}
    assert "definitely_not_registered_tool" in denied_ids
    assert "system_text_generation" in allowed_ids
    assert result["denied_new"] >= 1


@pytest.mark.asyncio
async def test_gate_denied_recalled_candidate_remains_candidate_but_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    monkeypatch.setattr(
        api,
        "_recall_creator_tool_candidates",
        lambda file_specs, top_k: (
            [
                {"tool_id": "system_text_generation", "recalled_for_capabilities": ["text"]},
                {"tool_id": "definitely_not_registered_tool", "recalled_for_capabilities": ["missing"]},
            ],
            "test",
        ),
    )

    result = await api._plan_final_tool_pool(
        skill_name="demo",
        file_specs=[{"path": "scripts/a.py", "required": True}],
    )

    assert result["recalled_candidate_tool_ids"] == ["system_text_generation", "definitely_not_registered_tool"]
    assert result["available_optional_tool_ids"] == ["system_text_generation"]
    assert result["unavailable_tool_ids"] == ["definitely_not_registered_tool"]
    assert result["planner_output"]["recalled_candidate_tool_ids"] == ["system_text_generation", "definitely_not_registered_tool"]
    assert result["planner_output"]["available_optional_tool_ids"] == ["system_text_generation"]
    assert result["planner_output"]["unavailable_tool_ids"] == ["definitely_not_registered_tool"]

    pool = load_tool_pool(tmp_path / "demo")
    allowed = {tool.tool_id for tool in pool.tools if tool.status == "allowed"}
    denied = {item.tool_id for item in pool.denied_requests}
    assert allowed == {"system_text_generation"}
    assert "definitely_not_registered_tool" in denied


def test_final_tool_pool_computed_patch_uses_recall_union_wording():
    source = inspect.getsource(api._plan_final_tool_pool)

    assert "passed Backend factual authorization checks" not in source
    assert "Final Tool Selector marked this Registry candidate" not in source
    assert "Backend factual authorization" in source
    assert "Passing Gate means optional availability" in source
    assert "function_item_owned_optional_recall_no_llm" in source


def test_first_round_semantic_judge_tool_augmentation_flow_is_unchanged():
    source = inspect.getsource(api._plan_tool_pool_patch_from_responsibility_feedback)

    assert "_creator_responsibility_feedback_recall_query" in source
    assert "_recall_creator_tool_candidates" in source
    assert "responsibility_feedback" in source
    assert "candidate_tool_catalog" in source


def test_responsibility_tool_expansion_refreshes_binding_and_import_observation_before_repair():
    source = inspect.getsource(api.generate_file)

    refresh_start = source.index('refreshed_tool_pool = load_tool_pool(settings.skills_path / skill_name)')
    refresh_end = source.index('except Exception as planning_exc', refresh_start)
    refresh_block = source[refresh_start:refresh_end]

    repair_start = source.index('repair_tool_pool_summary = {}', refresh_end)
    repair_end = source.index('if single_block_locator is None:', repair_start)
    repair_block = source[repair_start:repair_end]

    binding_refresh = 'current_skill_binding_payload = refreshed_binding.model_dump(mode="json")'
    context_refresh = 'function_execution_context = _build_current_function_execution_context()'
    guard_refresh = 'last_import_guard_result = guard_runtime_imports('

    assert binding_refresh in refresh_block
    assert context_refresh in refresh_block
    assert guard_refresh in refresh_block
    assert 'candidate or ""' in refresh_block
    assert 'current_skill_binding_payload' in refresh_block
    assert refresh_block.index(binding_refresh) < refresh_block.index(guard_refresh)
    assert refresh_block.index(context_refresh) < refresh_block.index(guard_refresh)
    assert 'responsibility_import_observation_refreshed' in refresh_block

    assert 'repair_current_file_binding = dict(current_skill_binding_payload)' in repair_block
    assert 'repair_tool_pool_summary = dict(current_tool_pool_summary)' in repair_block
    assert 'repair_import_guard_result' in repair_block
    assert 'load_tool_pool(' not in repair_block
    assert 'guard_runtime_imports(' not in repair_block


def test_refreshed_import_observation_failure_is_non_blocking():
    source = inspect.getsource(api.generate_file)

    refresh_start = source.index('refreshed_tool_pool = load_tool_pool(settings.skills_path / skill_name)')
    refresh_end = source.index('except Exception as planning_exc', refresh_start)
    refresh_block = source[refresh_start:refresh_end]

    assert 'except Exception as guard_exc' in refresh_block
    assert '"success": None' in refresh_block
    assert '"observation_error"' in refresh_block
    assert 'raise FileGenerationStageError' not in refresh_block
    assert '_file_done_error_sse' not in refresh_block


def test_tool_readiness_blockers_are_judge_observations_not_producer_failures():
    source = inspect.getsource(api.generate_file)
    observation_start = source.index('_, tool_blockers = _creator_tool_readiness_blockers')
    helper_start = source.index('def _build_current_function_execution_context', observation_start)
    observation_block = source[observation_start:helper_start]
    judge_start = source.index('responsibility_review = await _run_script_responsibility_review')
    judge_end = source.index('if not responsibility_review.get("passed"):', judge_start)
    judge_block = source[judge_start:judge_end]

    assert 'tool_readiness_observations = list(tool_blockers)' in observation_block
    assert 'producer_tool_readiness_observation' in observation_block
    assert '_file_done_error_sse' not in observation_block
    assert 'error_type="tool_not_ready"' not in observation_block
    assert '"tool_readiness_observations": list(tool_readiness_observations)' in judge_block


def test_initial_skill_binding_syncs_before_generate_prompt_build():
    source = inspect.getsource(api.generate_file)
    binding_created = source.index('current_skill_binding_payload = current_file_binding.model_dump(mode="json")')
    entry_sync = source.index('effective_skill_plan_entry = _with_current_skill_tool_binding', binding_created)
    prompt_build = source.index('_build_generate_file_prompt(', entry_sync)
    assert binding_created < entry_sync < prompt_build


def test_refreshed_binding_syncs_entry_before_context_guard_and_repair():
    source = inspect.getsource(api.generate_file)
    refresh_start = source.index('refreshed_tool_pool = load_tool_pool(settings.skills_path / skill_name)')
    refresh_end = source.index('except Exception as planning_exc', refresh_start)
    refresh_block = source[refresh_start:refresh_end]
    binding_refresh = 'current_skill_binding_payload = refreshed_binding.model_dump(mode="json")'
    entry_sync = 'effective_skill_plan_entry = _with_current_skill_tool_binding'
    context_refresh = 'function_execution_context = _build_current_function_execution_context()'
    guard_refresh = 'last_import_guard_result = guard_runtime_imports('
    repair_start = source.index('repair_tool_pool_summary = {}', refresh_end)
    assert refresh_block.index(binding_refresh) < refresh_block.index(entry_sync)
    assert refresh_block.index(entry_sync) < refresh_block.index(context_refresh)
    assert refresh_block.index(context_refresh) < refresh_block.index(guard_refresh)
    assert refresh_end < repair_start


@pytest.mark.asyncio
async def test_available_optional_tools_are_bound_only_to_owning_function_item(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    monkeypatch.setattr(
        api,
        "_recall_creator_tool_candidates",
        lambda file_specs, top_k: (
            [
                {"tool_id": "system_text_generation", "recalled_for_capabilities": ["write"]},
                {"tool_id": "system_image_generation", "recalled_for_capabilities": ["draw"]},
            ],
            "test",
        ),
    )

    result = await api._plan_final_tool_pool(
        skill_name="demo",
        file_specs=[
            {"path": "scripts/write.py", "required": True, "required_capabilities": ["write"]},
            {"path": "scripts/draw.py", "required": True, "required_capabilities": ["draw"]},
        ],
    )

    assert result["available_optional_tool_ids"] == ["system_image_generation", "system_text_generation"]
    assert result["tool_bindings_by_file"] == {
        "scripts/write.py": ["system_text_generation"],
        "scripts/draw.py": ["system_image_generation"],
    }


@pytest.mark.asyncio
async def test_final_tool_pool_persists_file_bindings_and_compat_aliases(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    monkeypatch.setattr(
        api,
        "_recall_creator_tool_candidates",
        lambda file_specs, top_k: (
            [{"tool_id": "system_text_generation", "recalled_for_capabilities": ["write"]}],
            "test",
        ),
    )

    result = await api._plan_final_tool_pool(
        skill_name="demo",
        file_specs=[{"path": "scripts/write.py", "required": True, "required_capabilities": ["write"]}],
    )

    assert result["desired_tool_ids"] == ["system_text_generation"]
    assert result["authorized_tool_ids"] == ["system_text_generation"]
    assert result["planner_output"]["desired_tool_ids"] == ["system_text_generation"]
    assert result["planner_output"]["authorized_tool_ids"] == ["system_text_generation"]

    pool = load_tool_pool(tmp_path / "demo")
    raw_binding = api.get_file_binding(pool, "scripts/write.py", raw=True)
    projected_binding = api.get_file_binding(pool, "scripts/write.py")
    assert raw_binding is not None
    assert raw_binding.allowed_tool_ids == ["system_text_generation"]
    assert projected_binding is not None
    assert "script_argv_guard" in projected_binding.allowed_tool_ids
    assert "system_text_generation" in projected_binding.allowed_tool_ids
    assert api.tool_pool_snapshot(pool)["file_bindings"][0]["target_file"] == "scripts/write.py"


@pytest.mark.asyncio
async def test_persisted_file_binding_keeps_selected_tool_env_dependencies_and_import_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    monkeypatch.setattr(
        api,
        "_recall_creator_tool_candidates",
        lambda file_specs, top_k: (
            [{"tool_id": "system_image_generation", "recalled_for_capabilities": ["draw"]}],
            "test",
        ),
    )

    result = await api._plan_final_tool_pool(
        skill_name="demo",
        file_specs=[{"path": "scripts/draw.py", "required": True, "required_capabilities": ["draw"]}],
    )

    binding = api.get_file_binding(result["tool_pool"], "scripts/draw.py", raw=True)
    assert binding is not None
    assert binding.allowed_tool_ids == ["system_image_generation"]
    assert binding.allowed_import_paths
    assert binding.allowed_function_imports
    assert isinstance(binding.required_env, list)
    assert isinstance(binding.dependencies, list)


def test_file_binding_helper_preserves_selected_tool_env_dependencies_and_import_metadata():
    pool = ToolPoolModel(
        tools=[
            ToolPoolTool(
                tool_id="custom_tool",
                status="allowed",
                allowed_import_paths=["pkg.helpers"],
                allowed_function_imports=["pkg.helpers.run"],
                required_env=["CUSTOM_TOKEN"],
                dependencies=["custom-lib"],
            )
        ]
    )

    binding = api._creator_file_binding_from_optional_tool_ids(
        pool=pool,
        target_file="scripts/custom.py",
        allowed_tool_ids=["custom_tool"],
    )

    assert binding.allowed_tool_ids == ["custom_tool"]
    assert binding.allowed_import_paths == ["pkg.helpers"]
    assert binding.allowed_function_imports == ["pkg.helpers.run"]
    assert binding.required_env == ["CUSTOM_TOKEN"]
    assert binding.dependencies == ["custom-lib"]


def _binding(target_file, tool_ids, available_tools=None):
    return ToolPoolFileBinding(
        target_file=target_file,
        allowed_tool_ids=list(tool_ids),
        primary_tool_ids=list(tool_ids),
        available_tools=list(available_tools or []),
    )


def test_responsibility_feedback_merges_only_target_file_binding(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir(parents=True)
    save_tool_pool(
        skill_dir,
        ToolPoolModel(
            tools=[ToolPoolTool(tool_id="system_text_generation", status="allowed")],
            file_bindings=[
                _binding("scripts/a.py", ["system_text_generation"]),
                _binding("scripts/b.py", ["system_text_generation"]),
            ],
        ),
    )

    api._apply_planner_tool_pool_patch(
        skill_name="demo",
        planner_output={
            "tool_pool_patch": {
                "add_tool_requests": [{"candidate_tool_id": "system_image_generation"}],
                "remove_tool_requests": [],
                "affected_files": ["scripts/a.py"],
            }
        },
        source_phase="responsibility_feedback",
        allow_remove=False,
    )

    pool = load_tool_pool(skill_dir)
    by_file = {binding.target_file: binding for binding in pool.file_bindings}
    assert by_file["scripts/a.py"].allowed_tool_ids == ["system_text_generation", "system_image_generation"]
    assert by_file["scripts/b.py"].allowed_tool_ids == ["system_text_generation"]
    assert {tool.tool_id for tool in pool.tools if tool.status == "allowed"} == {
        "system_text_generation",
        "system_image_generation",
    }


def test_patch_without_target_file_does_not_clear_existing_bindings(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir(parents=True)
    original_bindings = [
        _binding("scripts/a.py", ["system_text_generation"]),
        _binding("scripts/b.py", ["system_image_generation"]),
    ]
    save_tool_pool(
        skill_dir,
        ToolPoolModel(
            tools=[ToolPoolTool(tool_id="system_text_generation", status="allowed")],
            file_bindings=original_bindings,
        ),
    )

    api._apply_planner_tool_pool_patch(
        skill_name="demo",
        planner_output={
            "tool_pool_patch": {
                "add_tool_requests": [{"candidate_tool_id": "system_text_generation"}],
                "remove_tool_requests": [],
                "affected_files": [],
            }
        },
        source_phase="responsibility_feedback",
        allow_remove=False,
    )

    pool = load_tool_pool(skill_dir)
    assert [binding.model_dump(mode="json") for binding in pool.file_bindings] == [
        binding.model_dump(mode="json") for binding in original_bindings
    ]


def test_script_guard_projection_fields_match_for_persisted_and_fallback_bindings():
    persisted_pool = ToolPoolModel(file_bindings=[_binding("scripts/a.py", [])])
    fallback_pool = ToolPoolModel()

    persisted = api.get_file_binding(persisted_pool, "scripts/a.py")
    fallback = api.get_file_binding(fallback_pool, "scripts/a.py")

    for binding in (persisted, fallback):
        assert binding is not None
        assert "script_argv_guard" in binding.allowed_tool_ids
        assert "strict_json_argv_guard" in binding.allowed_helper_imports
        assert "backend.services.runtime_tools" in binding.allowed_import_paths
        assert "strict_json_argv_guard" in binding.allowed_function_imports
        assert "backend.services.runtime_tools.strict_json_argv_guard" in binding.allowed_function_imports
        guard_tools = [
            tool for tool in binding.available_tools
            if tool.get("tool_id") == "script_argv_guard"
            and tool.get("function_name") == "strict_json_argv_guard"
        ]
        assert len(guard_tools) == 1


def test_e2e_callable_repair_context_uses_current_file_binding_and_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_path", tmp_path)
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir(parents=True)
    pool = ToolPoolModel(
        tools=[
            ToolPoolTool(tool_id="system_text_generation", status="allowed"),
            ToolPoolTool(tool_id="system_image_generation", status="allowed"),
        ]
    )
    pool.file_bindings = [
        api._creator_file_binding_from_optional_tool_ids(
            pool=pool,
            target_file="scripts/a.py",
            allowed_tool_ids=["system_text_generation"],
        ),
        api._creator_file_binding_from_optional_tool_ids(
            pool=pool,
            target_file="scripts/b.py",
            allowed_tool_ids=["system_image_generation"],
        ),
    ]
    save_tool_pool(skill_dir, pool)

    context = api._build_e2e_callable_repair_context(skill_name="demo", target_file="scripts/a.py")
    tool_ids = {tool.get("tool_id") for tool in context.get("available_tools", [])}
    assert "system_text_generation" in tool_ids
    assert "system_image_generation" not in tool_ids
    assert "script_argv_guard" in tool_ids

    save_tool_pool(skill_dir, ToolPoolModel(tools=[ToolPoolTool(tool_id="system_text_generation", status="allowed")]))
    fallback_context = api._build_e2e_callable_repair_context(skill_name="demo", target_file="scripts/a.py")
    fallback_tool_ids = {tool.get("tool_id") for tool in fallback_context.get("available_tools", [])}
    assert "system_text_generation" in fallback_tool_ids
    assert "script_argv_guard" in fallback_tool_ids
