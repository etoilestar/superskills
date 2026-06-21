"""Tests for backend/services/llm_proxy.py."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx


# ---------------------------------------------------------------------------
# _build_chat_completions_url
# ---------------------------------------------------------------------------

def test_url_plain_base():
    from backend.services.llm_proxy import _build_chat_completions_url

    assert _build_chat_completions_url("http://localhost:11434") == \
        "http://localhost:11434/v1/chat/completions"


def test_url_v1_suffix():
    from backend.services.llm_proxy import _build_chat_completions_url

    assert _build_chat_completions_url("http://localhost:11434/v1") == \
        "http://localhost:11434/v1/chat/completions"


def test_url_full_path_unchanged():
    from backend.services.llm_proxy import _build_chat_completions_url

    full = "http://localhost:11434/v1/chat/completions"
    assert _build_chat_completions_url(full) == full


def test_url_trailing_slash_stripped():
    from backend.services.llm_proxy import _build_chat_completions_url

    assert _build_chat_completions_url("http://localhost:11434/") == \
        "http://localhost:11434/v1/chat/completions"


# ---------------------------------------------------------------------------
# _build_payload
# ---------------------------------------------------------------------------

def test_build_payload_minimal():
    from backend.services.llm_proxy import _build_payload
    from backend.config import settings

    payload = _build_payload(messages=[{"role": "user", "content": "hi"}], model="test", stream=False)
    assert payload["model"] == "test"
    assert payload["stream"] is False
    assert payload["messages"][0]["content"] == "hi"
    # When temperature/max_tokens are None they should not appear in payload
    if settings.temperature is None:
        assert "temperature" not in payload
    if settings.max_tokens is None:
        assert "max_tokens" not in payload


def test_build_payload_with_temperature():
    from backend.services import llm_proxy
    from backend.config import settings

    with patch.object(settings, "temperature", 0.7):
        payload = llm_proxy._build_payload(messages=[], model="m", stream=False)

    assert payload["temperature"] == 0.7


def test_build_payload_with_max_tokens():
    from backend.services import llm_proxy
    from backend.config import settings

    with patch.object(settings, "max_tokens", 512):
        payload = llm_proxy._build_payload(messages=[], model="m", stream=False)

    assert payload["max_tokens"] == 512


# ---------------------------------------------------------------------------
# _auth_headers
# ---------------------------------------------------------------------------

def test_auth_headers_no_key():
    from backend.services.llm_proxy import _auth_headers
    from backend.config import settings

    with patch.object(settings, "llm_api_key", None), \
         patch.object(settings, "openai_api_key", None), \
         patch.dict("os.environ", {}, clear=False):
        # Remove env vars if present
        import os
        env_backup = {k: os.environ.pop(k) for k in ("LLM_API_KEY", "OPENAI_API_KEY") if k in os.environ}
        try:
            result = _auth_headers()
        finally:
            os.environ.update(env_backup)

    assert result == {}


def test_auth_headers_with_key():
    from backend.services.llm_proxy import _auth_headers
    from backend.config import settings

    with patch.object(settings, "llm_api_key", "sk-test123"):
        result = _auth_headers()

    assert "Authorization" in result
    assert "sk-test123" in result["Authorization"]


# ---------------------------------------------------------------------------
# complete_chat_once — mocked HTTP
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_chat_once_success():
    from backend.services.llm_proxy import complete_chat_once

    response_data = {
        "model": "test-model",
        "choices": [{"message": {"content": "Hello from LLM"}}]
    }

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=response_data)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await complete_chat_once([{"role": "user", "content": "hi"}], "test-model")

    assert result == "Hello from LLM"


@pytest.mark.asyncio
async def test_complete_chat_once_empty_choices():
    from backend.services.llm_proxy import complete_chat_once

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"choices": []})

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await complete_chat_once([{"role": "user", "content": "hi"}], "test-model")

    assert result == ""


@pytest.mark.asyncio
async def test_complete_chat_once_http_error():
    from backend.services.llm_proxy import complete_chat_once

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("404", request=MagicMock(), response=MagicMock(status_code=404))
    )
    mock_response.json = MagicMock(return_value={})

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        with pytest.raises(httpx.HTTPStatusError):
            await complete_chat_once([{"role": "user", "content": "hi"}], "test-model")




def test_models_match_exact_and_canonical_suffix():
    from backend.services.model_router import _models_match

    assert _models_match("qwen", "qwen")
    assert _models_match("qwen", "ollama/qwen")
    assert not _models_match("qwen", "llama")

# ---------------------------------------------------------------------------
# check_connection — mocked HTTP
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_connection_success():
    from backend.services.llm_proxy import check_connection

    model_data = {"data": [{"id": "model-a"}, {"id": "model-b"}]}
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=model_data)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await check_connection(deep=True)

    assert result["connected"] is True
    assert "model-a" in result["models"]


@pytest.mark.asyncio
async def test_check_connection_failure():
    from backend.services.llm_proxy import check_connection

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client_cls.return_value = mock_client

        result = await check_connection(deep=True)

    assert result["connected"] is False
    assert "error" in result


@pytest.mark.asyncio
async def test_check_connection_quick_returns_unknown_without_cache(monkeypatch):
    from backend.services import llm_proxy

    llm_proxy._llm_health_cache["result"] = None
    llm_proxy._llm_health_cache["checked_at"] = 0.0
    scheduled = {"value": False}

    def fake_schedule():
        scheduled["value"] = True

    monkeypatch.setattr(llm_proxy, "_schedule_llm_health_refresh", fake_schedule)

    with patch("httpx.AsyncClient") as mock_client_cls:
        result = await llm_proxy.check_connection(deep=False)

    assert result == {
        "connected": None,
        "models": [],
        "stale": True,
        "refreshing": True,
        "status": "unknown",
    }
    assert scheduled["value"] is True
    mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_check_connection_deep_refreshes_synchronously():
    from backend.services import llm_proxy

    llm_proxy._llm_health_cache["result"] = None
    llm_proxy._llm_health_cache["checked_at"] = 0.0
    model_data = {"data": [{"id": "deep-model"}]}
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=model_data)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await llm_proxy.check_connection(deep=True)

    assert result["connected"] is True
    assert result["models"] == ["deep-model"]
    mock_client.get.assert_awaited_once()
