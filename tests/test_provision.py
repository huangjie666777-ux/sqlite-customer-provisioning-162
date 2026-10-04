import importlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient


def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'one')")
    conn.commit()
    conn.close()


@pytest.fixture()
def provision_client(tmp_path, monkeypatch):
    make_db(tmp_path / "a.db")
    make_db(tmp_path / "b.db")
    cfg = {
        "review_enabled": True,
        "aliases": {"a": "a.db", "b": "b.db"},
        "review_credentials": {
            "alice-token": "alice",
            "bob-token": "bob",
        },
    }
    (tmp_path / "aliases.json").write_text(json.dumps(cfg))
    monkeypatch.setenv("MIGRATION_CONFIG", str(tmp_path / "aliases.json"))
    monkeypatch.setenv("MIGRATION_CHECKPOINT_DIR", str(tmp_path / "cps"))
    monkeypatch.setenv("MIGRATION_BATCH_DIR", str(tmp_path / "batches"))
    monkeypatch.setenv("MIGRATION_REVIEW_DIR", str(tmp_path / "reviews"))
    monkeypatch.setenv("MIGRATION_PROVISION_DIR", str(tmp_path / "provisions"))
    monkeypatch.setenv("MIGRATION_PROVISION_ROOT", str(tmp_path / "newdbs"))
    import app.config
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.main)
    return TestClient(app.main.app), tmp_path, app.main


def headers(token):
    return {"authorization": f"Bearer {token}"}


def release_payload():
    return {
        "databases": [
            {
                "alias": "a",
                "expected_version": 0,
                "scripts": [
                    {
                        "version": 1,
                        "description": "create items",
                        "sql": "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT);",
                    },
                    {
                        "version": 2,
                        "description": "add price",
                        "sql": "ALTER TABLE items ADD COLUMN price REAL;",
                    },
                ],
            }
        ]
    }


def succeeded_release(client):
    r = client.post("/releases", json=release_payload(), headers=headers("alice-token"))
    assert r.status_code == 201, r.text
    rel = r.json()
    r = client.post(
        f"/releases/{rel['release_id']}/approve",
        json={"content_sha256": rel["content_sha256"]},
        headers=headers("bob-token"),
    )
    assert r.status_code == 200
    r = client.post(
        f"/releases/{rel['release_id']}/execute", headers=headers("alice-token")
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "succeeded"
    return rel


def provision_payload(rel, key="key-1", new_alias="customer_x"):
    return {
        "release_id": rel["release_id"],
        "source_alias": "a",
        "new_alias": new_alias,
        "idempotency_key": key,
    }


def test_provision_initializes_empty_db_and_routes_alias(provision_client):
    client, tmp_path, _ = provision_client
    rel = succeeded_release(client)
    r = client.post(
        "/provisions", json=provision_payload(rel), headers=headers("alice-token")
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["person"] == "alice"
    assert body["new_alias"] == "customer_x"
    assert body["target_version"] == 2
    assert body["source_content_sha256"] == rel["content_sha256"]

    # 新别名立即可查版本：完整历史从版本 0 应用。
    r = client.get("/databases/customer_x/version")
    assert r.status_code == 200
    data = r.json()
    assert data["current_version"] == 2
    assert [a["version"] for a in data["applied"]] == [1, 2]

    # 空库初始化：不复制源库业务数据。
    conn = sqlite3.connect(tmp_path / "newdbs" / "customer_x.db")
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    conn.close()

    # 新别名可用于检查点。
    r = client.post("/databases/customer_x/checkpoints")
    assert r.status_code == 201


def test_idempotent_replay_returns_same_record(provision_client):
    client, _, _ = provision_client
    rel = succeeded_release(client)
    first = client.post(
        "/provisions", json=provision_payload(rel), headers=headers("alice-token")
    )
    assert first.status_code == 201
    replay = client.post(
        "/provisions", json=provision_payload(rel), headers=headers("alice-token")
    )
    assert replay.status_code == 200
    assert replay.json()["provision_id"] == first.json()["provision_id"]

    # 同键换内容拒绝。
    changed = provision_payload(rel, new_alias="other")
    r = client.post("/provisions", json=changed, headers=headers("alice-token"))
    assert r.status_code == 409
    assert r.json()["code"] == "idempotency_conflict"


def test_alias_conflict_rejected(provision_client):
    client, _, _ = provision_client
    rel = succeeded_release(client)
    # 与静态别名冲突。
    payload = provision_payload(rel, key="k-static", new_alias="a")
    r = client.post("/provisions", json=payload, headers=headers("alice-token"))
    assert r.status_code == 409
    assert r.json()["code"] == "alias_conflict"
    # 与已开通别名冲突。
    client.post("/provisions", json=provision_payload(rel), headers=headers("alice-token"))
    payload = provision_payload(rel, key="k-2")
    r = client.post("/provisions", json=payload, headers=headers("alice-token"))
    assert r.status_code == 409
    assert r.json()["code"] == "alias_conflict"


def test_unsucceeded_release_rejected(provision_client):
    client, _, _ = provision_client
    r = client.post("/releases", json=release_payload(), headers=headers("alice-token"))
    rel = r.json()
    r = client.post(
        "/provisions", json=provision_payload(rel), headers=headers("alice-token")
    )
    assert r.status_code == 409
    assert r.json()["code"] == "invalid_release_state"


def test_provision_requires_credentials_and_forbids_extra_fields(provision_client):
    client, _, _ = provision_client
    rel = succeeded_release(client)
    r = client.post("/provisions", json=provision_payload(rel))
    assert r.status_code == 401
    payload = provision_payload(rel, key="k-sql")
    payload["sql"] = "DROP TABLE t;"
    payload["path"] = "/tmp/evil.db"
    r = client.post("/provisions", json=payload, headers=headers("alice-token"))
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_provision"


def test_release_execute_requires_credentials(provision_client):
    client, _, _ = provision_client
    r = client.post("/releases", json=release_payload(), headers=headers("alice-token"))
    rel = r.json()
    client.post(
        f"/releases/{rel['release_id']}/approve",
        json={"content_sha256": rel["content_sha256"]},
        headers=headers("bob-token"),
    )
    r = client.post(f"/releases/{rel['release_id']}/execute")
    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"


def test_restart_recovers_succeeded_alias(provision_client, monkeypatch):
    client, tmp_path, _ = provision_client
    rel = succeeded_release(client)
    r = client.post(
        "/provisions", json=provision_payload(rel), headers=headers("alice-token")
    )
    assert r.status_code == 201

    import app.config
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.main)
    client2 = TestClient(app.main.app)
    r = client2.get("/databases/customer_x/version")
    assert r.status_code == 200
    assert r.json()["current_version"] == 2
    r = client2.get("/provisions", headers=headers("alice-token"))
    assert r.status_code == 200
    assert r.json()["provisions"][0]["status"] == "succeeded"
