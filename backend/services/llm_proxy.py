import httpx
import json
import os
import logging
from collections.abc import AsyncGenerator, Callable

from ..config import settings
from .model_router import _models_match


logger = logging.getLogger(__name__)


def _resolve_api_key() -> str | None:
    """Resolve the LLM API key from config or environment, returning None if absent."""
    return (
        settings.llm_api_key
        or settings.openai_api_key
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )


def _resolve_image_api_key() -> str | None:
    """Resolve the image-generation API key independently from the LLM key."""
    return (
        settings.image_api_key
        or os.environ.get("IMAGE_API_KEY")
        or _resolve_api_key()
    )


def _auth_headers() -> dict:
    """Return Authorization header when an OpenAI API key is configured."""
    api_key = _resolve_api_key()
    if api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


def _build_chat_completions_url(base_url: str) -> str:
    """Build OpenAI-compatible chat completions URL.

    Supported forms:
    - http://127.0.0.1:11434
    - http://127.0.0.1:11434/v1
    - http://127.0.0.1:11434/v1/chat/completions
    """
    base = base_url.rstrip("/")

    if base.endswith("/v1/chat/completions"):
        return base

    if base.endswith("/v1"):
        return f"{base}/chat/completions"

    return f"{base}/v1/chat/completions"

def _build_image_generations_url(base_url: str) -> str:
    """
    Build OpenAI-compatible image generations URL.

    Supported forms:
    - http://127.0.0.1:11435
    - http://127.0.0.1:11435/v1
    - http://127.0.0.1:11435/v1/images/generations
    """
    base = base_url.rstrip("/")

    if base.endswith("/v1/images/generations"):
        return base

    if base.endswith("/v1"):
        return f"{base}/images/generations"

    return f"{base}/v1/images/generations"

def _get_api_key() -> str:
    """Ollama ignores the key, but OpenAI-compatible services usually expect one."""
    return _resolve_api_key() or "ollama"


def _build_payload(
    *,
    messages: list[dict],
    model: str,
    stream: bool,
) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }

    if settings.temperature is not None:
        payload["temperature"] = settings.temperature

    if settings.max_tokens is not None:
        payload["max_tokens"] = settings.max_tokens

    return payload


def _ack_response_model(*, expected_model: str, actual_model: str | None, phase: str) -> None:
    """Log and optionally enforce provider model acknowledgement."""
    matched = _models_match(expected_model, actual_model) if actual_model else None
    logger.info(
        "[LLM][ack] phase=%s expected_model=%s actual_model=%s matched=%s",
        phase,
        expected_model,
        actual_model or "",
        matched,
    )
    if settings.model_ack_strict and actual_model and not matched:
        raise ValueError(
            f"LLM provider returned model {actual_model!r}, expected {expected_model!r}"
        )


def _build_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_get_api_key()}",
    }


def _build_image_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_resolve_image_api_key() or 'ollama'}",
    }


async def complete_chat_once(messages: list[dict], model: str) -> str:
    """Non-streaming chat completion.

    用于 metadata 阶段的静默模型调用。
    """
    url = _build_chat_completions_url(settings.llm_base_url)
    payload = _build_payload(messages=messages, model=model, stream=False)
    headers = _build_headers()
    timeout = float(settings.llm_timeout_seconds)

    logger.info("[LLM][once] request model=%s url=%s messages=%d", model, url, len(messages))

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    _ack_response_model(expected_model=model, actual_model=data.get("model"), phase="once")

    choices = data.get("choices") or []
    if not choices:
        logger.warning("[LLM][once] empty choices")
        return ""

    choice = choices[0]

    message = choice.get("message") or {}
    content = message.get("content")

    if not content:
        content = choice.get("text") or ""

    logger.info(
        "[LLM][once] response length=%d content=\n%s",
        len(content),
        content,
    )

    return content

async def generate_image_once(
    *,
    prompt: str,
    model: str,
    size: str | None = None,
    response_format: str = "b64_json",
) -> dict:
    """Call OpenAI-compatible image generation API."""
    url = _build_image_generations_url(settings.image_base_url)
    headers = _build_image_headers()
    timeout = float(settings.llm_timeout_seconds)

    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": size or settings.image_size,
        "response_format": response_format,
    }

    logger.info("[IMAGE][once] request model=%s url=%s size=%s", model, url, payload["size"])

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()

async def stream_chat(
    messages: list[dict],
    model: str,
    model_ack_callback: Callable[[dict], None] | None = None,
) -> AsyncGenerator[str, None]:
    """Stream chat completion from Ollama/OpenAI-compatible API."""
    url = _build_chat_completions_url(settings.llm_base_url)
    payload = _build_payload(messages=messages, model=model, stream=True)
    headers = _build_headers()
    timeout = float(settings.llm_timeout_seconds)

    logger.info("[LLM][stream] request model=%s url=%s messages=%d", model, url, len(messages))

    full_content: list[str] = []
    ack_sent = False

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            url,
            json=payload,
            headers=headers,
        ) as response:
            response.raise_for_status()

            async for line in response.aiter_lines():
                if not line:
                    continue

                if not line.startswith("data: "):
                    continue

                data_str = line[6:].strip()

                if data_str == "[DONE]":
                    break

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    logger.warning("[LLM][stream] invalid json line=%s", data_str[:500])
                    continue

                if not ack_sent:
                    actual_model = data.get("model")
                    _ack_response_model(expected_model=model, actual_model=actual_model, phase="stream")
                    if model_ack_callback is not None:
                        model_ack_callback({
                            "expected_model": model,
                            "actual_model": actual_model or "",
                            "matched": _models_match(model, actual_model) if actual_model else None,
                        })
                    ack_sent = True

                choices = data.get("choices") or []
                if not choices:
                    continue

                choice = choices[0]

                delta = choice.get("delta") or {}
                content = delta.get("content")

                if not content:
                    message = choice.get("message") or {}
                    content = message.get("content")

                if content:
                    full_content.append(content)
                    yield content

    if not ack_sent:
        _ack_response_model(expected_model=model, actual_model=None, phase="stream")

    final_text = "".join(full_content)

    logger.info(
        "[LLM][stream] response done length=%d content=\n%s",
        len(final_text),
        final_text,
    )


async def check_connection() -> dict:
    """Check if the LLM backend is reachable and return available models."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{settings.llm_base_url.rstrip('/')}/v1/models",
                headers=_auth_headers(),
            )
            response.raise_for_status()
            data = response.json()
            models = [m["id"] for m in data.get("data", [])]
            return {"connected": True, "models": models}
    except Exception as exc:
        return {"connected": False, "error": str(exc), "models": []}
