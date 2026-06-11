"""Runtime helpers exposed to generated Skill scripts.

These helpers keep platform-specific model contracts out of generated Skills:
- text/translation requests use the LLM endpoint and TEXT_MODEL;
- Stable Diffusion image requests use the image endpoint and IMAGE_MODEL;
- image responses are normalized to files under OUTPUT_DIR.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

from .runtime_tools import (
    analyze_image_with_vision,
    api_get,
    api_post,
    build_pdf_report,
    create_docx,
    create_pdf,
    create_pptx,
    create_wechat_draft,
    describe_database_table,
    extract_pdf_text,
    fetch_url_text,
    images_to_pdf,
    list_database_tables,
    merge_pdfs,
    ocr_image,
    publish_wechat_draft,
    query_database_readonly,
    read_docx_text,
    read_pptx_text,
    read_spreadsheet,
    upload_wechat_media,
    web_search,
)


logger = logging.getLogger(__name__)

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")

# A valid 1x1 transparent PNG used only during Creator trial runs.  Real
# sandbox execution never uses this branch unless SKILL_TRIAL_RUN is set.
_TRIAL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/"
    "gL+X2cAAAAASUVORK5CYII="
)


def _timeout_seconds() -> float:
    raw = _env("LLM_TIMEOUT_SECONDS", "6000")
    try:
        return float(raw)
    except ValueError:
        return 6000.0


def _build_chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _build_image_generations_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1/images/generations"):
        return base
    if base.endswith("/v1"):
        return f"{base}/images/generations"
    return f"{base}/v1/images/generations"


def _post_json(url: str, *, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_timeout_seconds()) as response:  # nosec: platform-configured URL
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name) or default


def _required_env(name: str) -> str:
    value = _env(name).strip()
    if not value:
        raise RuntimeError(
            f"Required platform environment variable {name} is not set. "
            "Skill scripts must run through the platform sandbox/runtime."
        )
    return value


def _llm_api_key() -> str:
    return (
        _env("LLM_API_KEY")
        or _env("OPENAI_API_KEY")
        or "ollama"
    )


def _image_api_key() -> str:
    return (
        _env("IMAGE_API_KEY")
        or _llm_api_key()
    )


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


def _strip_model_text(text: str) -> str:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip().strip('"').strip("'").strip()


def translate_image_prompt_to_english(topic: str) -> str:
    """Silently translate/rewrite an image topic into an English SD prompt.

    This is intentionally platform-side logic. Generated Skills should pass the
    user's topic here; they should not contain their own Chinese-to-English
    prompt engineering or call TEXT_MODEL directly for image prompts.
    """
    topic = str(topic or "").strip()
    if not topic:
        raise ValueError("image topic/prompt is empty")

    if os.environ.get("SKILL_TRIAL_RUN") == "1":
        return "a cinematic watercolor cat under a warm sunset"

    url = _build_chat_completions_url(_required_env("LLM_BASE_URL"))
    model = _required_env("TEXT_MODEL")
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You rewrite image-generation topics for Stable Diffusion. "
                    "Return exactly one concise English prompt. Do not explain."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Rewrite this topic into an English Stable Diffusion prompt. "
                    "Preserve the user's intent and concrete visual details.\n\n"
                    f"Topic: {topic}"
                ),
            },
        ],
    }
    try:
        data = _post_json(url, payload=payload, headers=_headers(_llm_api_key()))
    except Exception:
        if _CJK_RE.search(topic):
            raise
        logger.warning("image prompt rewrite failed; using original English prompt", exc_info=True)
        return topic

    choices = data.get("choices") or []
    if not choices:
        if _CJK_RE.search(topic):
            raise ValueError("TEXT_MODEL returned no prompt rewrite choices")
        return topic

    message = choices[0].get("message") or {}
    rewritten = _strip_model_text(message.get("content") or choices[0].get("text") or "")
    if not rewritten:
        if _CJK_RE.search(topic):
            raise ValueError("TEXT_MODEL returned an empty prompt rewrite")
        return topic
    return rewritten


def _write_image_bytes(*, image_bytes: bytes, output_dir: Path, filename_prefix: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_prefix = _SAFE_FILENAME_RE.sub("-", filename_prefix.strip() or "image").strip("-._") or "image"
    path = output_dir / f"{safe_prefix}-{int(time.time() * 1000)}.png"
    path.write_bytes(image_bytes)
    return path


def _decode_image_response(data: dict[str, Any]) -> tuple[bytes, str]:
    items = data.get("data") or []
    if not items or not isinstance(items[0], dict):
        raise ValueError("image API response missing data[0]")

    first = items[0]
    b64_json = first.get("b64_json")
    if b64_json:
        return base64.b64decode(str(b64_json)), "b64_json"

    raise ValueError(f"Image API did not return b64_json: {data}")



def generate_text_with_llm(prompt: str, *, system: str = "", temperature: float = 0.7) -> str:
    """Generate text with the platform configured LLM/TEXT_MODEL.

    Generated Skill scripts use this helper instead of embedding API details.
    During Creator trial runs it returns deterministic non-empty text so script
    validation can verify plumbing without network access.
    """
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("text generation prompt is empty")

    if os.environ.get("SKILL_TRIAL_RUN") == "1":
        return f"Generated text for: {prompt}"

    url = _build_chat_completions_url(_required_env("LLM_BASE_URL"))
    payload = {
        "model": _required_env("TEXT_MODEL"),
        "stream": False,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system or "You are a helpful writing assistant."},
            {"role": "user", "content": prompt},
        ],
    }
    data = _post_json(url, payload=payload, headers=_headers(_llm_api_key()))
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("TEXT_MODEL returned no choices")
    message = choices[0].get("message") or {}
    text = _strip_model_text(message.get("content") or choices[0].get("text") or "")
    if not text:
        raise ValueError("TEXT_MODEL returned empty text")
    return text

def generate_stable_diffusion_image(
    topic: str,
    *,
    output_dir: str | os.PathLike[str] | None = None,
    filename_prefix: str = "image",
    size: str | None = None,
) -> dict[str, Any]:
    """Generate an image with the platform Stable Diffusion model.

    Returns a JSON-serializable dict containing the English prompt, image model,
    image path, and response source.  Generated scripts can print this dict as
    stdout JSON so the sandbox can expose the image as an output file.
    """
    out_dir = Path(output_dir or _env("OUTPUT_DIR", "outputs"))
    english_prompt = translate_image_prompt_to_english(topic)

    if os.environ.get("SKILL_TRIAL_RUN") == "1":
        image_path = _write_image_bytes(
            image_bytes=base64.b64decode(_TRIAL_PNG_B64),
            output_dir=out_dir,
            filename_prefix=filename_prefix,
        )
        return {
            "prompt": english_prompt,
            "model": _required_env("IMAGE_MODEL"),
            "image_path": str(image_path),
            "source": "trial",
        }

    # IMAGE_MODEL is injected by the platform from settings.image_model.
    # Do not hardcode Stable Diffusion model names in generated Skills.
    image_model = _required_env("IMAGE_MODEL")
    image_base_url = _required_env("IMAGE_BASE_URL")
    image_size = size or _required_env("IMAGE_SIZE")
    url = _build_image_generations_url(image_base_url)
    payload = {
        "model": image_model,
        "prompt": english_prompt,
        "n": 1,
        "size": image_size,
        "response_format": "b64_json",
    }
    data = _post_json(url, payload=payload, headers=_headers(_image_api_key()))

    image_bytes, source = _decode_image_response(data)
    image_path = _write_image_bytes(
        image_bytes=image_bytes,
        output_dir=out_dir,
        filename_prefix=filename_prefix,
    )
    mime_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
    return {
        "prompt": english_prompt,
        "model": image_model,
        "size": image_size,
        "image_path": str(image_path),
        "mime_type": mime_type,
        "source": source,
    }


def print_json(data: dict[str, Any]) -> None:
    """Print compact UTF-8 JSON for generated scripts."""
    print(json.dumps(data, ensure_ascii=False))
