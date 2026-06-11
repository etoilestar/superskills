"""Whitelist-restricted HTTP helpers for generated Skills."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import urllib.parse
import urllib.request
from typing import Any

_MAX_RESPONSE_BYTES = 512 * 1024


def _trial() -> bool:
    return os.environ.get("SKILL_TRIAL_RUN") == "1"


def _timeout() -> float:
    try:
        return float(os.environ.get("API_FETCH_TIMEOUT") or 20)
    except ValueError:
        return 20.0


def _validate_url(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only http/https URLs are allowed")
    allowed = {host.strip().lower() for host in (os.environ.get("API_FETCH_ALLOWED_HOSTS") or "").split(",") if host.strip()}
    host = parsed.hostname.lower()
    if not allowed or host not in allowed:
        raise ValueError("url host is not in API_FETCH_ALLOWED_HOSTS")
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("private, localhost, metadata, and reserved addresses are blocked")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("could not resolve url host") from exc
    return urllib.parse.urlunparse(parsed)


def _request(method: str, url: str, *, headers: dict | None = None, params: dict | None = None, json_body: dict | None = None) -> dict[str, Any]:
    if _trial():
        parsed = urllib.parse.urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
        allowed = {item.strip().lower() for item in (os.environ.get("API_FETCH_ALLOWED_HOSTS") or "").split(",") if item.strip()}
        if parsed.scheme not in {"http", "https"} or not host or (allowed and host not in allowed):
            raise ValueError("url host is not allowed")
        safe_url = urllib.parse.urlunparse(parsed)
        if params:
            separator = "&" if parsed.query else "?"
            safe_url = f"{safe_url}{separator}{urllib.parse.urlencode(params)}"
        return {"url": safe_url, "status_code": 200, "headers": {}, "text": "Mock API response during SKILL_TRIAL_RUN.", "json": {"mock": True}}

    safe_url = _validate_url(url)
    if params:
        separator = "&" if urllib.parse.urlparse(safe_url).query else "?"
        safe_url = f"{safe_url}{separator}{urllib.parse.urlencode(params)}"
    data = json.dumps(json_body or {}).encode("utf-8") if method == "POST" else None
    request = urllib.request.Request(safe_url, data=data, headers={str(k): str(v) for k, v in (headers or {}).items()}, method=method)
    if method == "POST":
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=_timeout()) as response:  # nosec: validated allowlist URL
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
        text = raw[:_MAX_RESPONSE_BYTES].decode("utf-8", errors="replace")
        content_type = response.headers.get("content-type", "")
        parsed_json = None
        if "json" in content_type:
            try:
                parsed_json = json.loads(text)
            except Exception:
                parsed_json = None
        return {"url": safe_url, "status_code": response.status, "headers": dict(response.headers), "text": text, "json": parsed_json, "truncated": len(raw) > _MAX_RESPONSE_BYTES}


def api_get(url: str, headers: dict | None = None, params: dict | None = None) -> dict[str, Any]:
    return _request("GET", url, headers=headers, params=params)


def api_post(url: str, headers: dict | None = None, json_body: dict | None = None) -> dict[str, Any]:
    return _request("POST", url, headers=headers, json_body=json_body)
