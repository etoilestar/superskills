"""Tests for backend/config.py — Settings model validation and defaults."""

import pytest
from pathlib import Path
from unittest.mock import patch


def test_default_settings_load():
    """Settings should load with default values without errors."""
    from backend.config import settings

    assert settings.llm_base_url.startswith("http")
    assert settings.default_model
    assert settings.skill_resource_max_chars > 0
    assert settings.skill_command_timeout > 0
    assert settings.llm_timeout_seconds > 0


def test_new_fields_declared():
    """All fields that were previously accessed via getattr should now be proper attrs."""
    from backend.config import Settings

    fields = Settings.model_fields
    expected = [
        "llm_base_url",
        "default_model",
        "openai_api_key",
        "llm_api_key",
        "planner_model",
        "validator_model",
        "text_model",
        "code_model",
        "image_model",
        "vision_model",
        "model_routing_json",
        "code_file_extensions",
        "image_task_keywords",
        "model_ack_strict",
        "temperature",
        "max_tokens",
        "llm_timeout_seconds",
        "kernel_path",
        "skills_path",
        "skill_resource_max_chars",
        "skill_command_timeout",
    ]
    for name in expected:
        assert name in fields, f"Missing field: {name}"


def test_planner_model_has_stronger_default():
    from backend.config import settings

    assert settings.planner_model == "qwen3:30b-instruct"


def test_validator_model_has_fast_default():
    from backend.config import settings

    assert settings.validator_model == "qwen3:8b"


def test_capability_models_have_local_defaults():
    from backend.config import settings

    assert settings.text_model == "qwen3:30b"
    assert settings.code_model == "qwen3-coder:30b"
    assert settings.image_model == "stable-diffusion-2-1-base"
    assert settings.vision_model == "qwen3-vl:32b"


def test_temperature_defaults_to_none():
    from backend.config import settings

    assert settings.temperature is None


def test_max_tokens_defaults_to_none():
    from backend.config import settings

    assert settings.max_tokens is None


def test_skills_path_created_if_missing(tmp_path):
    """skills_path should be created automatically when it doesn't exist."""
    from backend.config import Settings

    kernel = tmp_path / "kernel"
    kernel.mkdir()
    # A non-existent skills path should be auto-created
    new_skills = tmp_path / "skills_new"
    assert not new_skills.exists()
    s = Settings(kernel_path=kernel, skills_path=new_skills)
    assert new_skills.is_dir()


def test_kernel_path_must_exist(tmp_path):
    """kernel_path validator should raise when the directory doesn't exist."""
    from backend.config import Settings

    missing_kernel = tmp_path / "no_kernel_here"
    with pytest.raises(Exception):
        Settings(kernel_path=missing_kernel, skills_path=tmp_path / "skills")


def test_uppercase_env_model_settings_override_defaults(monkeypatch, tmp_path):
    """Project .env-style uppercase names should populate Settings fields explicitly."""
    from backend.config import Settings

    kernel = tmp_path / "kernel"
    kernel.mkdir()
    monkeypatch.setenv("LLM_BASE_URL", "http://172.18.127.67:11434")
    monkeypatch.setenv("DEFAULT_MODEL", "qwen3:30b")
    monkeypatch.setenv("TEXT_MODEL", "qwen-text")
    monkeypatch.setenv("CODE_MODEL", "qwen-code")
    monkeypatch.setenv("PLANNER_MODEL", "qwen-plan")
    monkeypatch.setenv("VALIDATOR_MODEL", "qwen-validate")
    monkeypatch.setenv("IMAGE_MODEL", "qwen-image")
    monkeypatch.setenv("IMAGE_API_KEY", "image-key")
    monkeypatch.setenv("VISION_MODEL", "qwen-vision")
    monkeypatch.setenv("SKILL_COMMAND_TIMEOUT", "180")

    s = Settings(kernel_path=kernel, skills_path=tmp_path / "skills")

    assert s.llm_base_url == "http://172.18.127.67:11434"
    assert s.default_model == "qwen3:30b"
    assert s.text_model == "qwen-text"
    assert s.code_model == "qwen-code"
    assert s.planner_model == "qwen-plan"
    assert s.validator_model == "qwen-validate"
    assert s.image_model == "qwen-image"
    assert s.image_api_key == "image-key"
    assert s.vision_model == "qwen-vision"
    assert s.skill_command_timeout == 180


def test_settings_reads_project_root_env_file():
    """The configured .env path should be pinned to the repository root."""
    from backend.config import PROJECT_ROOT, Settings

    assert Settings.model_config["env_file"] == PROJECT_ROOT / ".env"
    assert Settings.model_config["extra"] == "ignore"
