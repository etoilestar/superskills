"""Controlled vision/OCR helpers for generated Skills."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import urllib.request
from pathlib import Path
from typing import Any

_ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _trial() -> bool:
    return os.environ.get("SKILL_TRIAL_RUN") == "1"


def _allowed_roots() -> list[Path]:
    roots = [Path.cwd()]
    for name in ("SKILL_WORKDIR", "SKILL_DIR", "INPUT_DIR", "UPLOAD_DIR", "OUTPUT_DIR"):
        if os.environ.get(name):
            roots.append(Path(os.environ[name]))
    roots.extend([Path.cwd() / "inputs", Path.cwd() / "assets", Path.cwd() / "uploads"])
    return [root.expanduser().resolve() for root in roots]


def _safe_image_path(image_path: str) -> Path:
    path = Path(image_path).expanduser().resolve()
    if path.suffix.lower() not in _ALLOWED_EXTENSIONS:
        raise ValueError("Only png/jpg/jpeg/webp images are supported")
    if not path.is_file():
        raise FileNotFoundError("image file does not exist")
    if path.stat().st_size > _MAX_IMAGE_BYTES:
        raise ValueError("image file is too large")
    if not any(path == root or path.is_relative_to(root) for root in _allowed_roots()):
        raise ValueError("image_path must stay under the skill workdir, inputs, assets, uploads, or OUTPUT_DIR")
    return path


def _vision_endpoint() -> str:
    base = (os.environ.get("VISION_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
    if not base:
        raise RuntimeError("VISION_BASE_URL or OPENAI_BASE_URL is not set")
    if base.endswith("/v1/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _timeout() -> float:
    try:
        return float(os.environ.get("VISION_TIMEOUT") or 60)
    except ValueError:
        return 60.0


def analyze_image_with_vision(image_path: str, prompt: str = "Describe this image.") -> dict[str, Any]:
    """Analyze an existing image through an OpenAI-compatible vision endpoint."""
    path = _safe_image_path(image_path)
    if _trial():
        return {"image_path": str(path), "description": "Mock image description during SKILL_TRIAL_RUN.", "ocr_text": "", "model": "trial"}
    model = os.environ.get("VISION_MODEL") or ""
    if not model:
        raise RuntimeError("VISION_MODEL is not set")
    api_key = os.environ.get("VISION_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    if not api_key:
        raise RuntimeError("VISION_API_KEY or OPENAI_API_KEY is not set")
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    data_url = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": str(prompt or "Describe this image.")},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}],
    }
    request = urllib.request.Request(
        _vision_endpoint(),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_timeout()) as response:  # nosec: platform-configured URL
        body = json.loads(response.read().decode("utf-8", errors="replace"))
    choices = body.get("choices") or []
    message = (choices[0].get("message") if choices else {}) or {}
    description = message.get("content") or ""
    return {"image_path": str(path), "description": str(description), "ocr_text": "", "model": model}


def ocr_image(image_path: str, language: str = "auto") -> dict[str, Any]:
    """OCR an existing image via the vision endpoint."""
    prompt = f"Extract all readable text from this image. Language hint: {language or 'auto'}."
    result = analyze_image_with_vision(image_path, prompt=prompt)
    if _trial():
        result["ocr_text"] = "Mock OCR text during SKILL_TRIAL_RUN."
    else:
        result["ocr_text"] = result.get("description", "")
    result["language"] = language or "auto"
    return result
