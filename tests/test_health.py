from fastapi.testclient import TestClient

from app.api import main


def test_health_ok(monkeypatch):
    monkeypatch.setattr(main.connection, "ping", lambda: True)
    response = TestClient(main.app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "falkordb": True}


def test_health_reports_falkordb_down(monkeypatch):
    def fail() -> bool:
        raise ConnectionError("refused")

    monkeypatch.setattr(main.connection, "ping", fail)
    response = TestClient(main.app).get("/health")
    assert response.status_code == 503
    assert response.json()["falkordb"] is False
