"""Controlled retrieval and read-only database helpers for generated Skills."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from typing import Any

_MAX_SEARCH_RESULTS = 10
_MAX_FETCH_CHARS = 12000
_FORBIDDEN_SQL = re.compile(r"\b(insert|update|delete|drop|alter|truncate|create|replace|grant|revoke|vacuum|attach|detach)\b", re.I)


def _trial() -> bool:
    return os.environ.get("SKILL_TRIAL_RUN") == "1"


def _timeout(env_name: str, default: float) -> float:
    try:
        return float(os.environ.get(env_name) or default)
    except ValueError:
        return default


def _json_request(url: str, *, headers: dict[str, str] | None = None, timeout: float = 20.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec: platform-configured URL
        body = response.read(1024 * 1024).decode("utf-8", errors="replace")
    return json.loads(body)


def web_search(query: str, top_k: int = 5, language: str | None = None) -> dict[str, Any]:
    """Search with SearchXNG/SearXNG and return JSON-serializable results."""
    query = str(query or "").strip()
    if not query:
        raise ValueError("query is empty")
    top_k = max(1, min(int(top_k or 5), _MAX_SEARCH_RESULTS))
    if _trial():
        return {
            "query": query,
            "results": [
                {
                    "title": f"Mock result {index} for {query}",
                    "url": f"https://example.com/mock/{index}",
                    "snippet": "Mock search result returned during SKILL_TRIAL_RUN.",
                    "source": "trial",
                }
                for index in range(1, top_k + 1)
            ],
        }

    base_url = (os.environ.get("SEARCHXNG_BASE_URL") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("SEARCHXNG_BASE_URL is not set")
    params = {"q": query, "format": "json"}
    if language:
        params["language"] = language
    if os.environ.get("SEARCHXNG_ENGINE"):
        params["engines"] = os.environ["SEARCHXNG_ENGINE"]
    url = f"{base_url}/search?{urllib.parse.urlencode(params)}"
    headers = {"Accept": "application/json"}
    if os.environ.get("SEARCHXNG_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['SEARCHXNG_API_KEY']}"
    data = _json_request(url, headers=headers, timeout=_timeout("SEARCHXNG_TIMEOUT", 20.0))
    results = []
    for item in list(data.get("results") or [])[:top_k]:
        results.append({
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "snippet": str(item.get("content") or item.get("snippet") or ""),
            "source": str(item.get("engine") or item.get("source") or "searchxng"),
        })
    return {"query": query, "results": results}


def fetch_url_text(url: str, max_chars: int = _MAX_FETCH_CHARS) -> dict[str, Any]:
    """Fetch URL text with bounded response size."""
    url = str(url or "").strip()
    if not url:
        raise ValueError("url is empty")
    max_chars = max(1, min(int(max_chars or _MAX_FETCH_CHARS), _MAX_FETCH_CHARS))
    if _trial():
        return {"url": url, "text": "Mock fetched page text during SKILL_TRIAL_RUN.", "truncated": False}
    request = urllib.request.Request(url, headers={"User-Agent": "superskills-runtime/1.0"})
    with urllib.request.urlopen(request, timeout=_timeout("SEARCHXNG_TIMEOUT", 20.0)) as response:  # nosec: caller-provided capability-gated URL
        raw = response.read(max_chars * 4 + 1)
    text = raw.decode("utf-8", errors="replace")[:max_chars]
    return {"url": url, "text": text, "truncated": len(raw) > len(text.encode("utf-8", errors="ignore"))}


def _database_url() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    dialect = os.environ.get("DB_DIALECT") or ""
    host = os.environ.get("DB_HOST") or ""
    name = os.environ.get("DB_NAME") or ""
    user = urllib.parse.quote(os.environ.get("DB_USER") or "")
    password = urllib.parse.quote(os.environ.get("DB_PASSWORD") or "")
    port = f":{os.environ.get('DB_PORT')}" if os.environ.get("DB_PORT") else ""
    auth = f"{user}:{password}@" if user or password else ""
    if dialect and host and name:
        return f"{dialect}://{auth}{host}{port}/{name}"
    return ""


def _validate_sql(sql: str) -> str:
    statement = str(sql or "").strip().rstrip(";")
    if not re.match(r"^(select|with)\b", statement, re.I):
        raise ValueError("Only SELECT/WITH readonly statements are allowed")
    if ";" in statement or _FORBIDDEN_SQL.search(statement):
        raise ValueError("Only a single readonly SELECT/WITH statement is allowed")
    return statement


def _limited_sql(sql: str, limit: int) -> str:
    limit = max(1, min(int(limit or 100), 1000))
    if re.search(r"\blimit\s+\d+\b", sql, re.I):
        return sql
    return f"{sql} LIMIT {limit}"


def query_database_readonly(sql: str, params: dict | None = None, limit: int = 100) -> dict[str, Any]:
    """Run a bounded read-only query without exposing credentials."""
    limit = max(1, min(int(limit or 100), 1000))
    statement = _limited_sql(_validate_sql(sql), limit)
    if _trial():
        return {"columns": ["id", "name"], "rows": [{"id": 1, "name": "mock"}], "row_count": 1, "truncated": False}
    url = _database_url()
    if not url:
        raise RuntimeError("DATABASE_URL or DB_* environment variables are not set")
    try:
        if url.startswith("sqlite:///"):
            db_path = url.removeprefix("sqlite:///")
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(statement, params or {}).fetchmany(limit + 1)
                columns = list(rows[0].keys()) if rows else []
                data_rows = [dict(row) for row in rows[:limit]]
                return {"columns": columns, "rows": data_rows, "row_count": len(data_rows), "truncated": len(rows) > limit}
        from sqlalchemy import create_engine, text  # type: ignore

        engine = create_engine(url)
        with engine.connect() as conn:
            result = conn.execute(text(statement), params or {})
            rows = result.fetchmany(limit + 1)
            columns = list(result.keys())
        return {"columns": columns, "rows": [dict(row._mapping) for row in rows[:limit]], "row_count": min(len(rows), limit), "truncated": len(rows) > limit}
    except Exception as exc:
        raise RuntimeError("readonly database query failed") from exc


def list_database_tables() -> dict[str, Any]:
    if _trial():
        return {"tables": ["mock_table"]}
    return query_database_readonly("SELECT name FROM sqlite_master WHERE type='table'", limit=1000) if _database_url().startswith("sqlite:///") else {"tables": []}


def describe_database_table(table_name: str) -> dict[str, Any]:
    table_name = re.sub(r"[^A-Za-z0-9_]", "", str(table_name or ""))
    if not table_name:
        raise ValueError("table_name is empty")
    if _trial():
        return {"table_name": table_name, "columns": [{"name": "id", "type": "integer"}]}
    if _database_url().startswith("sqlite:///"):
        result = query_database_readonly(f"SELECT name, type FROM pragma_table_info('{table_name}')", limit=1000)
        return {"table_name": table_name, "columns": result.get("rows", [])}
    return {"table_name": table_name, "columns": []}
