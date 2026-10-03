from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_unknown_alias():
    r = client.get("/databases/nope/version")
    assert r.status_code == 404


def test_invalid_manifest_chain():
    payload = {
        "expected_version": 0,
        "scripts": [
            {"version": 2, "description": "d", "sql": "CREATE TABLE x(a);"}
        ],
    }
    r = client.post("/databases/demo/migrate", json=payload)
    assert r.status_code == 422


def test_blank_script_rejected():
    payload = {
        "expected_version": 0,
        "scripts": [{"version": 1, "description": "d", "sql": "   "}],
    }
    r = client.post("/databases/demo/migrate", json=payload)
    assert r.status_code == 422


def test_body_size_limit():
    huge = "x" * (3 * 1024 * 1024)
    r = client.post(
        "/databases/demo/migrate",
        content=huge,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413
