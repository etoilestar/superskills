"""Small persistent runtime overlays for Creator planner and reviewer calls."""
from __future__ import annotations

import json
import os
import tempfile
import logging
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from ..config import settings
from .llm_proxy import complete_chat_once

ROLES = ("planner", "reviewer")
_lock = RLock()
logger = logging.getLogger(__name__)


def _profile_path() -> Path:
    return settings.governance_path / "creator-model-profiles.json"


def _empty() -> dict:
    return {role: {"base_url": "", "api_key": "", "model": "", "max_tokens": None} for role in ROLES}


def _load() -> dict:
    with _lock:
        try:
            data = json.loads(_profile_path().read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        result = _empty()
        for role in ROLES:
            if isinstance(data.get(role), dict):
                result[role].update({key: data[role].get(key, result[role][key]) for key in result[role]})
        return result


def _save(data: dict) -> None:
    path = _profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        fd, temporary = tempfile.mkstemp(prefix=".creator-model-profiles-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


@dataclass(frozen=True)
class CreatorModelProfile:
    base_url: str
    api_key: str | None
    model: str
    max_tokens: int | None


def resolve_creator_model_profile(
    role: Literal["planner", "reviewer"], *, fallback_model: str,
) -> CreatorModelProfile:
    if role not in ROLES:
        raise ValueError(f"Unknown Creator model profile: {role}")
    saved = _load()[role]
    return CreatorModelProfile(
        base_url=str(saved.get("base_url") or settings.llm_base_url),
        api_key=str(saved["api_key"]) if saved.get("api_key") else None,
        model=str(saved.get("model") or fallback_model),
        max_tokens=saved.get("max_tokens") if saved.get("max_tokens") is not None else settings.max_tokens,
    )


async def complete_creator_role_once(
    messages: list[dict], role: Literal["planner", "reviewer"], *, fallback_model: str,
    stage: str = "creator",
) -> str:
    profile = resolve_creator_model_profile(role, fallback_model=fallback_model)
    logger.info("[Creator][model] stage=%s role=%s resolved_model=%s provider_base_url=%s", stage, role, profile.model, profile.base_url)
    return await complete_chat_once(messages, profile.model, base_url=profile.base_url, api_key=profile.api_key, max_tokens=profile.max_tokens)


class ProfileUpdate(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    max_tokens: int | None = None
    clear_api_key: bool = False
    restore_defaults: bool = False

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        if value == "":
            return ""
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return value

    @field_validator("model")
    @classmethod
    def valid_model(cls, value: str | None) -> str | None:
        if value == "":
            return ""
        if value is not None and not value.strip():
            raise ValueError("model must be a non-empty string")
        return value

    @field_validator("max_tokens")
    @classmethod
    def valid_tokens(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("max_tokens must be a positive integer")
        return value


def _public(role: str, profile: dict) -> dict:
    return {"role": role, "base_url": profile["base_url"], "model": profile["model"], "max_tokens": profile["max_tokens"], "api_key_configured": bool(profile["api_key"])}


router = APIRouter(prefix="/api/creator/model-profiles", tags=["creator"])


@router.get("")
def get_profiles() -> dict:
    profiles = _load()
    return {"profiles": {role: _public(role, profiles[role]) for role in ROLES}}


@router.put("/{role}")
def put_profile(role: str, update: ProfileUpdate) -> dict:
    if role not in ROLES:
        raise HTTPException(status_code=404, detail="Unknown Creator model profile")
    profiles = _load()
    current = profiles[role]
    if update.restore_defaults:
        profiles[role] = _empty()[role]
    else:
        values = update.model_dump(exclude_unset=True)
        for field in ("base_url", "model", "max_tokens"):
            if field in values:
                current[field] = values[field] if values[field] is not None else (None if field == "max_tokens" else "")
        if update.clear_api_key:
            current["api_key"] = ""
        elif update.api_key:
            current["api_key"] = update.api_key
    _save(profiles)
    return {"profile": _public(role, profiles[role])}
