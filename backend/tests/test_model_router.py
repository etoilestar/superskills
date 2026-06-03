"""Tests for capability-aware model routing."""

from unittest.mock import patch


def test_creator_scripts_route_to_code_model():
    from backend.config import settings
    from backend.services.model_router import route_creator_file_model

    with patch.object(settings, "code_model", "coder-model"):
        route = route_creator_file_model(
            file_path="scripts/main.py",
            purpose="处理数据",
            requested_model="general-model",
        )

    assert route.task == "code"
    assert route.model == "coder-model"


def test_creator_image_intent_routes_to_image_model():
    from backend.config import settings
    from backend.services.model_router import route_creator_file_model

    with patch.object(settings, "image_model", "image-model"):
        route = route_creator_file_model(
            file_path="assets/poster-prompt.md",
            purpose="生成海报图像素材说明",
            requested_model="general-model",
        )

    assert route.task == "image"
    assert route.model == "image-model"


def test_requested_text_model_is_fallback_when_no_specialized_model():
    from backend.config import settings
    from backend.services.model_router import route_creator_file_model

    with patch.object(settings, "code_model", None), patch.object(settings, "image_model", None), patch.object(settings, "text_model", None):
        route = route_creator_file_model(
            file_path="SKILL.md",
            purpose="说明工作流",
            requested_model="selected-model",
        )

    assert route.task == "text"
    assert route.model == "selected-model"


def test_model_routing_json_can_override_creator_path():
    from backend.config import settings
    from backend.services.model_router import route_creator_file_model

    routing_json = '{"creator_paths": {"references/*.md": "doc-model"}}'
    with patch.object(settings, "model_routing_json", routing_json):
        route = route_creator_file_model(
            file_path="references/spec.md",
            purpose="参考文档",
            requested_model="general-model",
        )

    assert route.model == "doc-model"
    assert "override" in route.reason


def test_sandbox_plan_code_action_keeps_final_response_on_text_model():
    from backend.config import settings
    from backend.services.model_router import infer_sandbox_response_task, route_model

    plan = {"tasks": [{"action": "run_command", "command": "python scripts/build.py"}]}
    task = infer_sandbox_response_task(body_prompt="", user_text="运行脚本", plan=plan)
    with patch.object(settings, "text_model", "text-a"):
        route = route_model(task, requested_model="general", reason="test")

    assert task == "text"
    assert route.model == "text-a"


def test_sandbox_image_upload_routes_to_vision_model():
    from backend.config import settings
    from backend.services.model_router import infer_sandbox_response_task, route_model

    task = infer_sandbox_response_task(
        body_prompt="",
        user_text="分析这张图片",
        plan={},
        input_files=[{"filename": "photo.png", "path": "inputs/session/photo.png"}],
    )
    with patch.object(settings, "vision_model", "qwen-vl"):
        route = route_model(task, requested_model="general", reason="test")

    assert task == "vision"
    assert route.model == "qwen-vl"
