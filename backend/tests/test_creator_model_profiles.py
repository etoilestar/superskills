import asyncio
from unittest.mock import AsyncMock, patch


def _empty_profiles():
    return {
        "planner": {"base_url": "", "api_key": "", "model": "", "max_tokens": None, "temperature": None},
        "reviewer": {"base_url": "", "api_key": "", "model": "", "max_tokens": None, "temperature": None},
    }


def test_empty_profile_uses_each_callsite_fallback_model(tmp_path, monkeypatch):
    from backend.config import settings
    from backend.services import creator_model_profiles as profiles

    monkeypatch.setattr(settings, "governance_path", tmp_path)
    profiles._save(_empty_profiles())
    with patch.object(profiles, "complete_chat_once", new=AsyncMock(return_value="ok")) as call:
        asyncio.run(profiles.complete_creator_role_once([], "planner", fallback_model="route-original"))
        asyncio.run(profiles.complete_creator_role_once([], "reviewer", fallback_model="planner-original"))
    assert call.await_args_list[0].args[1] == "route-original"
    assert call.await_args_list[1].args[1] == "planner-original"


def test_profile_model_override_wins_and_provider_fields_remain_isolated(tmp_path, monkeypatch):
    from backend.config import settings
    from backend.services import creator_model_profiles as profiles

    monkeypatch.setattr(settings, "governance_path", tmp_path)
    profiles._save({
        "planner": {"base_url": "https://a.test", "api_key": "key-A", "model": "model-A", "max_tokens": 1234},
        "reviewer": {"base_url": "https://b.test", "api_key": "key-B", "model": "model-B", "max_tokens": 2345},
    })
    with patch.object(profiles, "complete_chat_once", new=AsyncMock(return_value="ok")) as call:
        asyncio.run(profiles.complete_creator_role_once([], "planner", fallback_model="planner-original"))
        asyncio.run(profiles.complete_creator_role_once([], "reviewer", fallback_model="reviewer-original"))
    assert call.await_args_list[0].args[1] == "model-A"
    assert call.await_args_list[0].kwargs == {"base_url": "https://a.test", "api_key": "key-A", "max_tokens": 1234, "temperature": 0.1}
    assert call.await_args_list[1].args[1] == "model-B"
    assert call.await_args_list[1].kwargs == {"base_url": "https://b.test", "api_key": "key-B", "max_tokens": 2345, "temperature": 0.0}


def test_empty_fields_clear_overrides_and_keep_api_key(tmp_path, monkeypatch):
    from backend.config import settings
    from backend.services import creator_model_profiles as profiles

    monkeypatch.setattr(settings, "governance_path", tmp_path)
    monkeypatch.setattr(settings, "llm_base_url", "http://global.test")
    monkeypatch.setattr(settings, "max_tokens", 321)
    profiles._save({"planner": {"base_url": "https://remote.test", "api_key": "key-A", "model": "model-A", "max_tokens": 12}, "reviewer": _empty_profiles()["reviewer"]})

    response = profiles.put_profile("planner", profiles.ProfileUpdate(base_url="", model="", max_tokens=None))
    assert response["profile"] == {"role": "planner", "base_url": "", "model": "", "max_tokens": None, "temperature": None, "api_key_configured": True}
    resolved = profiles.resolve_creator_model_profile("planner", fallback_model="original-model")
    assert (resolved.base_url, resolved.api_key, resolved.model, resolved.max_tokens) == ("http://global.test", "key-A", "original-model", 321)


def test_profile_update_empty_fields_is_accepted():
    from backend.services import creator_model_profiles as profiles

    update = profiles.ProfileUpdate(base_url="", model="", max_tokens=None)
    assert update.base_url == ""
    assert update.model == ""
    assert update.max_tokens is None


def test_public_profile_never_contains_api_key():
    from backend.services.creator_model_profiles import _public
    result = _public("planner", {"base_url": "https://example.test", "api_key": "top-secret", "model": "model", "max_tokens": 1})
    assert "top-secret" not in str(result)
    assert result["api_key_configured"] is True


def test_role_temperature_override_is_forwarded(tmp_path, monkeypatch, caplog):
    from backend.config import settings
    from backend.services import creator_model_profiles as profiles

    caplog.set_level("INFO")
    monkeypatch.setattr(settings, "governance_path", tmp_path)
    saved = _empty_profiles()
    saved["planner"]["temperature"] = 0.15
    profiles._save(saved)
    with patch.object(profiles, "complete_chat_once", new=AsyncMock(return_value="ok")) as call:
        asyncio.run(profiles.complete_creator_role_once(
            [], "planner", fallback_model="planner-model", stage="interface_plan",
        ))
    assert call.await_args.kwargs["temperature"] == 0.15
    assert "temperature=0.15" in caplog.text


def test_structural_role_defaults_and_role_override_wins(tmp_path, monkeypatch):
    from backend.config import settings
    from backend.services import creator_model_profiles as profiles

    monkeypatch.setattr(settings, "governance_path", tmp_path)
    monkeypatch.setattr(settings, "temperature", 0.2)
    saved = _empty_profiles()
    profiles._save(saved)
    assert profiles.resolve_creator_model_profile("planner", fallback_model="model").temperature == 0.1
    assert profiles.resolve_creator_model_profile("reviewer", fallback_model="model").temperature == 0.0
    saved["planner"]["temperature"] = 0.05
    profiles._save(saved)
    assert profiles.resolve_creator_model_profile(
        "planner", fallback_model="model"
    ).temperature == 0.05
