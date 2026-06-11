"""WeChat Official Account helpers with trial-mode mocks."""

from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_MAX_MEDIA_BYTES = 10 * 1024 * 1024


def _trial() -> bool:
    return os.environ.get("SKILL_TRIAL_RUN") == "1"


def _timeout() -> float:
    try:
        return float(os.environ.get("WECHAT_TIMEOUT") or 20)
    except ValueError:
        return 20.0


def _required_env(name: str) -> str:
    value = os.environ.get(name) or ""
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def _get_access_token() -> str:
    if _trial():
        return "mock_access_token"
    app_id = _required_env("WECHAT_APP_ID")
    app_secret = _required_env("WECHAT_APP_SECRET")
    cache_path = os.environ.get("WECHAT_ACCESS_TOKEN_CACHE_PATH")
    if cache_path:
        path = Path(cache_path).expanduser()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if float(data.get("expires_at") or 0) > time.time() + 60 and data.get("access_token"):
                    return str(data["access_token"])
            except Exception:
                pass
    params = urllib.parse.urlencode({"grant_type": "client_credential", "appid": app_id, "secret": app_secret})
    with urllib.request.urlopen(f"https://api.weixin.qq.com/cgi-bin/token?{params}", timeout=_timeout()) as response:  # nosec: fixed WeChat API URL
        data = json.loads(response.read().decode("utf-8", errors="replace"))
    token = data.get("access_token")
    if not token:
        raise RuntimeError("failed to get WeChat access token")
    if cache_path:
        path = Path(cache_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"access_token": token, "expires_at": time.time() + int(data.get("expires_in") or 7200)}, ensure_ascii=False), encoding="utf-8")
    return str(token)


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=_timeout()) as response:  # nosec: fixed WeChat API URL
        return json.loads(response.read().decode("utf-8", errors="replace"))


def upload_wechat_media(file_path: str, media_type: str = "image") -> dict[str, Any]:
    path = Path(file_path).expanduser().resolve()
    if _trial():
        return {"media_id": "mock_media_id", "type": media_type, "status": "mock_uploaded"}
    if not path.is_file():
        raise FileNotFoundError("media file does not exist")
    if path.stat().st_size > _MAX_MEDIA_BYTES:
        raise ValueError("media file is too large")
    if media_type == "image" and path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        raise ValueError("unsupported image format")
    token = _get_access_token()
    boundary = "----superskillswechatboundary"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="media"; filename="{path.name}"\r\n'.encode(),
        f"Content-Type: {mimetypes.guess_type(str(path))[0] or 'application/octet-stream'}\r\n\r\n".encode(),
        path.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    url = f"https://api.weixin.qq.com/cgi-bin/media/upload?access_token={urllib.parse.quote(token)}&type={urllib.parse.quote(media_type)}"
    request = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    with urllib.request.urlopen(request, timeout=_timeout()) as response:  # nosec: fixed WeChat API URL
        data = json.loads(response.read().decode("utf-8", errors="replace"))
    return {"media_id": data.get("media_id"), "type": data.get("type", media_type), "created_at": data.get("created_at"), "status": "uploaded"}


def create_wechat_draft(title: str, content_html: str, author: str = "", digest: str = "", cover_image_path: str | None = None) -> dict[str, Any]:
    if _trial():
        return {"draft_id": "mock_draft_id", "media_id": "mock_media_id" if cover_image_path else "", "url": None, "status": "draft_created"}
    media_id = ""
    if cover_image_path:
        media_id = str(upload_wechat_media(cover_image_path, "image").get("media_id") or "")
    token = _get_access_token()
    payload = {"articles": [{"title": title, "author": author, "digest": digest, "content": content_html, "thumb_media_id": media_id, "need_open_comment": 0, "only_fans_can_comment": 0}]}
    data = _post_json(f"https://api.weixin.qq.com/cgi-bin/draft/add?access_token={urllib.parse.quote(token)}", payload)
    return {"draft_id": data.get("media_id"), "media_id": media_id, "url": None, "status": "draft_created"}


def publish_wechat_draft(draft_id: str) -> dict[str, Any]:
    if _trial():
        return {"draft_id": draft_id, "publish_id": "mock_publish_id", "status": "published"}
    token = _get_access_token()
    data = _post_json(f"https://api.weixin.qq.com/cgi-bin/freepublish/submit?access_token={urllib.parse.quote(token)}", {"media_id": draft_id})
    return {"draft_id": draft_id, "publish_id": data.get("publish_id"), "status": "publish_submitted"}
