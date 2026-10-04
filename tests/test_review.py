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
def review_client(tmp_path, monkeypatch):
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
    import app.config
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.main)
    return TestClient(app.main.app), tmp_path, app.main


def release_payload():
    return {
        "databases": [
                {
                    "alias": "a",
                    "expected_version": 0,
                    "scripts": [
                        {
                            "version": 1,
                            "description": "add column",
                            "sql": "ALTER TABLE t ADD COLUMN n TEXT;",
                        }
                    ],
                }
            ]
    }


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def create_release(client, token="alice-token"):
    r = client.post("/releases", json=release_payload(), headers=headers(token))
    assert r.status_code == 201, r.text
    return r.json()


def test_review_mode_blocks_direct_entries(review_client):
    client, _, _ = review_client
    payload = {
        "expected_version": 0,
        "scripts": [{"version": 1, "description": "d", "sql": "ALTER TABLE t ADD COLUMN x TEXT;"}],
    }
    assert client.post("/databases/a/migrate", json=payload).status_code == 403
    assert client.post("/batches", json=release_payload()).status_code == 403
    restore = {"checkpoint_id": "cp_x", "expected_version": 0}
    assert client.post("/databases/a/restore", json=restore).status_code == 403


def test_credential_identity_self_review_and_digest(review_client):
    client, _, _ = review_client
    assert client.post("/releases", json=release_payload()).status_code == 401
    rel = create_release(client)
    assert rel["status"] == "pending"
    assert rel["author"] == "alice"
    assert len(rel["content_sha256"]) == 64

    r = client.post(
        f"/releases/{rel['release_id']}/approve",
        json={"content_sha256": rel["content_sha256"]},
        headers=headers("alice-token"),
    )
    assert r.status_code == 409
    assert r.json()["code"] == "self_review"

    r = client.post(
        f"/releases/{rel['release_id']}/reject",
        json={"content_sha256": "x"},
        headers=headers("bob-token"),
    )
    assert r.status_code == 409
    assert r.json()["code"] == "digest_mismatch"
    assert client.get(f"/releases/{rel['release_id']}", headers=headers("bob-token")).json()["status"] == "pending"


def test_other_person_approval_then_execute_and_repeat_returns_same_batch(review_client):
    client, _, main = review_client
    rel = create_release(client)
    r = client.post(
        f"/releases/{rel['release_id']}/approve",
        json={"content_sha256": rel["content_sha256"]},
        headers=headers("bob-token"),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "approved"

    r = client.post(f"/releases/{rel['release_id']}/execute", headers=headers("alice-token"))
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["status"] == "succeeded"
    batch_id = first["batch_id"]
    assert first["result"]["batch_id"] == batch_id

    r = client.post(f"/releases/{rel['release_id']}/execute", headers=headers("alice-token"))
    assert r.status_code == 200
    assert r.json()["batch_id"] == batch_id
    detail = client.get(f"/batches/{batch_id}").json()
    assert detail["status"] == "succeeded"


def test_execute_requires_credential(review_client):
    client, _, _ = review_client
    rel = create_release(client)
    client.post(
        f"/releases/{rel['release_id']}/approve",
        json={"content_sha256": rel["content_sha256"]},
        headers=headers("bob-token"),
    )
    r = client.post(f"/releases/{rel['release_id']}/execute")
    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"


def _execute_provisionable_release(client):
    payload = {
        "databases": [
            {
                "alias": "a",
                "expected_version": 0,
                "scripts": [
                    {
                        "version": 1,
                        "description": "create customer table",
                        "sql": "CREATE TABLE customer (id INTEGER PRIMARY KEY, name TEXT NOT NULL);",
                    }
                ],
            }
        ]
    }
    r = client.post("/releases", json=payload, headers=headers("alice-token"))
    assert r.status_code == 201
    rel = r.json()
    r = client.post(
        f"/releases/{rel['release_id']}/approve",
        json={"content_sha256": rel["content_sha256"]},
        headers=headers("bob-token"),
    )
    assert r.status_code == 200
    r = client.post(f"/releases/{rel['release_id']}/execute", headers=headers("alice-token"))
    assert r.status_code == 200, r.text
    return rel


def test_provision_empty_database_from_succeeded_release_and_idempotency(review_client):
    client, tmp_path, _ = review_client
    rel = _execute_provisionable_release(client)
    body = {
        "release_id": rel["release_id"],
        "source_alias": "a",
        "new_alias": "customer-one",
        "idempotency_key": "same-key",
    }
    r = client.post("/provisionings", json=body, headers=headers("alice-token"))
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["status"] == "succeeded"
    assert first["version"] == 1
    assert first["source_summary"][0]["sql_sha256"]
    assert "sql" not in first["source_summary"][0]

    r = client.get("/databases/customer-one/version")
    assert r.status_code == 200
    assert r.json()["current_version"] == 1

    db_file = tmp_path / "provisioned" / "customer-one.db"
    conn = sqlite3.connect(db_file)
    assert conn.execute("SELECT COUNT(*) FROM customer").fetchone()[0] == 0
    conn.close()

    r = client.post("/provisionings", json=body, headers=headers("alice-token"))
    assert r.status_code == 200
    assert r.json()["provisioning_id"] == first["provisioning_id"]

    changed = dict(body, new_alias="customer-two")
    r = client.post("/provisionings", json=changed, headers=headers("alice-token"))
    assert r.status_code == 409
    assert r.json()["code"] == "idempotency_conflict"
    assert not (tmp_path / "provisioned" / "customer-two.db").exists()


def test_provision_rejects_invalid_release_alias_and_traversal(review_client):
    client, _, _ = review_client
    rel = create_release(client)
    body = {
        "release_id": rel["release_id"],
        "source_alias": "a",
        "new_alias": "new-customer",
        "idempotency_key": "pending-key",
    }
    r = client.post("/provisionings", json=body, headers=headers("alice-token"))
    assert r.status_code == 409
    assert r.json()["code"] == "invalid_release_state"
    body["idempotency_key"] = "static-key"
    body["new_alias"] = "a"
    r = client.post("/provisionings", json=body, headers=headers("alice-token"))
    assert r.status_code == 409
    assert r.json()["code"] == "alias_conflict"
    body["idempotency_key"] = "traversal-key"
    body["new_alias"] = "../escape"
    r = client.post("/provisionings", json=body, headers=headers("alice-token"))
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_alias"


def test_provisioning_persists_and_restores_successful_alias(review_client):
    client, tmp_path, first_main = review_client
    rel = _execute_provisionable_release(client)
    body = {
        "release_id": rel["release_id"],
        "source_alias": "a",
        "new_alias": "restored-customer",
        "idempotency_key": "restored-key",
    }
    created = client.post("/provisionings", json=body, headers=headers("alice-token")).json()

    import app.config
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.main)
    restarted = TestClient(app.main.app)
    assert restarted.get("/databases/restored-customer/version").json()["current_version"] == 1
    r = restarted.get(f"/provisionings/{created['provisioning_id']}", headers=headers("alice-token"))
    assert r.status_code == 200
    assert r.json()["status"] == "succeeded"


def test_cancel_approved_release_blocks_execution(review_client):
    client, _, _ = review_client
    rel = create_release(client)
    client.post(
        f"/releases/{rel['release_id']}/approve",
        json={"content_sha256": rel["content_sha256"]},
        headers=headers("bob-token"),
    )
    r = client.post(f"/releases/{rel['release_id']}/cancel", headers=headers("alice-token"))
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"
    r = client.post(f"/releases/{rel['release_id']}/execute", headers=headers("alice-token"))
    assert r.status_code == 409
    assert r.json()["code"] == "invalid_release_state"


def test_database_changed_after_approval_is_rejected(review_client):
    client, tmp_path, main = review_client
    rel = create_release(client)
    digest = rel["content_sha256"]
    client.post(
        f"/releases/{rel['release_id']}/approve",
        json={"content_sha256": digest},
        headers=headers("bob-token"),
    )
    conn = sqlite3.connect(tmp_path / "a.db")
    conn.execute("CREATE TABLE __schema_migration_log__ (version INTEGER PRIMARY KEY, description TEXT NOT NULL, sql_sha256 TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO __schema_migration_log__ VALUES (1, 'old', ?)",
        ("0" * 64,),
    )
    conn.commit()
    conn.close()

    r = client.post(f"/releases/{rel['release_id']}/execute", headers=headers("alice-token"))
    assert r.status_code == 409
    body = r.json()
    assert body["status"] == "prepare_failed"
    assert body["result"]["code"] == "version_conflict"


def test_failed_release_is_terminal_and_not_rerun(review_client):
    client, _, _ = review_client
    payload = {
        "databases": [
            {
                "alias": "a",
                "expected_version": 0,
                "scripts": [
                    {"version": 1, "description": "bad", "sql": "INSERT INTO missing_table VALUES (1);"}
                ],
            }
        ]
    }
    r = client.post("/releases", json=payload, headers=headers("alice-token"))
    rel = r.json()
    client.post(
        f"/releases/{rel['release_id']}/approve",
        json={"content_sha256": rel["content_sha256"]},
        headers=headers("bob-token"),
    )
    r = client.post(f"/releases/{rel['release_id']}/execute", headers=headers("alice-token"))
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "compensated"
    batch_id = body["batch_id"]
    r = client.post(f"/releases/{rel['release_id']}/execute", headers=headers("alice-token"))
    assert r.status_code == 422
    assert r.json()["batch_id"] == batch_id


def test_pending_release_persists_across_restart(review_client, monkeypatch):
    client, tmp_path, first_main = review_client
    rel = create_release(client)
    import app.config
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.main)
    restarted = TestClient(app.main.app)
    r = restarted.get(f"/releases/{rel['release_id']}", headers=headers("bob-token"))
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
