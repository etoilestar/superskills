from fastapi.testclient import TestClient

from backend.main import app


def test_llm_health_quick_returns_unknown_without_waiting(monkeypatch):
    async def fake_check_connection(*, deep=False):
        assert deep is False
        return {"connected": None, "models": [], "stale": True, "refreshing": True, "status": "unknown"}

    monkeypatch.setattr("backend.routers.health.check_connection", fake_check_connection)

    response = TestClient(app).get("/api/health/llm")

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is None
    assert body["stale"] is True
    assert body["refreshing"] is True
    assert body["status"] == "unknown"


def test_llm_health_deep_passes_through_sync_refresh_flag(monkeypatch):
    calls = []

    async def fake_check_connection(*, deep=False):
        calls.append(deep)
        return {"connected": True, "models": ["deep-model"], "stale": False, "refreshing": False}

    monkeypatch.setattr("backend.routers.health.check_connection", fake_check_connection)

    response = TestClient(app).get("/api/health/llm?deep=true")

    assert response.status_code == 200
    assert calls == [True]
    assert response.json()["models"] == ["deep-model"]
