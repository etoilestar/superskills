"""Adapters for preserving Creator structured values as plain data."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def to_plain_data(value: Any) -> Any:
    """Return a JSON-compatible copy without interpreting its semantics."""
    if is_dataclass(value) and not isinstance(value, type):
        return to_plain_data(asdict(value))
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return to_plain_data(value.model_dump(mode="json"))
    if hasattr(value, "dict") and callable(value.dict):
        return to_plain_data(value.dict())
    if isinstance(value, dict):
        return {str(key): to_plain_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain_data(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value
