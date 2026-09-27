"""Phase 0 smoke tests.

These run without Docker and without a database: they prove the FastAPI app
imports, the meta routes answer, and — importantly — that the readiness probe
degrades gracefully instead of exploding when Postgres is unreachable.

    cd backend && .venv/Scripts/python -m pytest ../tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_root_identifies_the_service() -> None:
    res = client.get("/")
    assert res.status_code == 200
    body = res.json()
    assert body["service"] == "dogfood-api"
    assert body["status"] == "ok"


def test_openapi_schema_is_published() -> None:
    """API-First depends on this being true from the first commit, not the last."""
    res = client.get("/openapi.json")
    assert res.status_code == 200
    schema = res.json()
    assert schema["info"]["title"] == "Dogfood Hackathon Portal API"
    assert "/health" in schema["paths"]


def test_health_reports_database_state_without_crashing() -> None:
    """With no database reachable, /health must answer 200 and say so."""
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["api"] == "ok"
    assert body["database"] in {"ok", "error"}
