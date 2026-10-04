import importlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.batch import BatchCoordinator, BatchJournal, BatchRequest
from app.checkpoints import CheckpointStore
from app.engine import DatabaseRegistry, apply_manifest, status
from app.manifest import MigrationManifest


def make_db(path, table="t"):
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute(f"INSERT INTO {table} VALUES (1, 'one')")
    conn.commit()
    conn.close()


def item(alias, version, sql, expected=0):
    return {
        "alias": alias,
        "expected_version": expected,
        "scripts": [{"version": 1, "description": f"m{version}", "sql": sql}],
    }


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    make_db(db_a)
    make_db(db_b)
    cfg = tmp_path / "aliases.json"
    cfg.write_text(json.dumps({"aliases": {"a": "a.db", "b": "b.db"}}))
    monkeypatch.setenv("MIGRATION_CONFIG", str(cfg))
    monkeypatch.setenv("MIGRATION_CHECKPOINT_DIR", str(tmp_path / "cps"))
    monkeypatch.setenv("MIGRATION_BATCH_DIR", str(tmp_path / "batches"))
    import app.config
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.main)
    yield TestClient(app.main.app), tmp_path, app.main


def test_batch_success(client):
    c, tmp_path, main = client
    payload = {
        "databases": [
            item("a", 1, "ALTER TABLE t ADD COLUMN n TEXT;"),
            item("b", 1, "ALTER TABLE t ADD COLUMN n TEXT; INSERT INTO t VALUES (2, 'two', 'x');"),
        ]
    }
    r = c.post("/batches", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["batch_id"].startswith("batch_")
    dbs = {d["alias"]: d for d in body["databases"]}
    assert dbs["a"]["before_version"] == 0 and dbs["a"]["after_version"] == 1
    assert dbs["b"]["before_version"] == 0 and dbs["b"]["after_version"] == 1
    assert dbs["a"]["checkpoint_id"] and dbs["b"]["checkpoint_id"]

    # 版本与历史真实前进
    assert c.get("/databases/a/version").json()["current_version"] == 1
    assert c.get("/databases/b/version").json()["current_version"] == 1
    # 检查点可用既有接口查询
    cps = c.get("/databases/a/checkpoints").json()["checkpoints"]
    assert [cp["id"] for cp in cps] == [dbs["a"]["checkpoint_id"]]

    # 批次可追溯
    r = c.get(f"/batches/{body['batch_id']}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["status"] == "succeeded"
    phases = [e["phase"] for e in detail["events"]]
    assert phases == ["planned", "prepared", "prepared", "migrated", "migrated"]
    assert c.get("/batches").json()["batches"][0]["batch_id"] == body["batch_id"]
    assert c.get("/batches/batch_nope").status_code == 404


def test_batch_failure_compensates_in_reverse(client):
    c, tmp_path, main = client
    payload = {
        "databases": [
            item("a", 1, "ALTER TABLE t ADD COLUMN n TEXT;"),
            item("b", 1, "INSERT INTO nope VALUES (1);"),
        ]
    }
    r = c.post("/batches", json=payload)
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "compensated"
    assert body["failed_version"] == 1
    dbs = {d["alias"]: d for d in body["databases"]}
    assert dbs["a"]["status"] == "restored"
    assert dbs["a"]["checkpoint_id"]
    assert dbs["b"]["status"] == "failed"
    assert dbs["b"]["error"]

    # a 库结构、数据、历史全部回到检查点
    assert c.get("/databases/a/version").json()["current_version"] == 0
    conn = sqlite3.connect(tmp_path / "a.db")
    cols = [row[1] for row in conn.execute("PRAGMA table_info(t)")]
    rows = conn.execute("SELECT * FROM t").fetchall()
    conn.close()
    assert cols == ["id", "v"]
    assert rows == [(1, "one")]
    # 失败库保持原状态
    assert c.get("/databases/b/version").json()["current_version"] == 0

    detail = c.get(f"/batches/{body['batch_id']}").json()
    assert detail["status"] == "compensated"
    phases = [e["phase"] for e in detail["events"]]
    assert "migration_failed" in phases and "restored" in phases


def test_batch_not_executed_after_failure(client):
    c, tmp_path, main = client
    cfg = json.loads((tmp_path / "aliases.json").read_text())
    make_db(tmp_path / "c.db")
    cfg["aliases"]["c"] = "c.db"
    (tmp_path / "aliases.json").write_text(json.dumps(cfg))
    import app.config
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.main)
    c = TestClient(app.main.app)

    payload = {
        "databases": [
            item("a", 1, "INSERT INTO nope VALUES (1);"),
            item("c", 1, "ALTER TABLE t ADD COLUMN n TEXT;"),
        ]
    }
    r = c.post("/batches", json=payload)
    assert r.status_code == 422
    dbs = {d["alias"]: d for d in r.json()["databases"]}
    assert dbs["a"]["status"] == "failed"
    assert dbs["c"]["status"] == "not_executed"
    assert c.get("/databases/c/version").json()["current_version"] == 0


def test_batch_prepare_failure_no_upgrade(client):
    c, tmp_path, main = client
    payload = {
        "databases": [
            item("a", 1, "ALTER TABLE t ADD COLUMN n TEXT;"),
            item("b", 1, "ALTER TABLE t ADD COLUMN n TEXT;", expected=5),
        ]
    }
    r = c.post("/batches", json=payload)
    assert r.status_code == 409
    body = r.json()
    assert body["status"] == "prepare_failed"
    assert c.get("/databases/a/version").json()["current_version"] == 0
    assert c.get("/databases/b/version").json()["current_version"] == 0


def test_batch_validation(client):
    c, tmp_path, main = client
    assert c.post("/batches", json={"databases": []}).status_code == 422
    dup = {"databases": [item("a", 1, "CREATE TABLE x (i);")] * 2}
    assert c.post("/batches", json=dup).status_code == 422
    bad = {"databases": [item("a", 1, "ALTER TABLE t ADD COLUMN n TEXT;", expected=-1)]}
    assert c.post("/batches", json=bad).status_code == 422
    badchain = {
        "databases": [
            {
                "alias": "a",
                "expected_version": 0,
                "scripts": [{"version": 2, "description": "d", "sql": "CREATE TABLE x (i);"}],
            }
        ]
    }
    assert c.post("/batches", json=badchain).status_code == 422
    unknown = {"databases": [item("ghost", 1, "CREATE TABLE x (i);")]}
    assert c.post("/batches", json=unknown).status_code == 404


def test_batch_alias_same_file_rejected(client, tmp_path=None):
    c, tmp_path, main = client
    cfg = json.loads((tmp_path / "aliases.json").read_text())
    cfg["aliases"]["a2"] = "a.db"
    (tmp_path / "aliases.json").write_text(json.dumps(cfg))
    import app.config
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.main)
    c = TestClient(app.main.app)
    payload = {
        "databases": [
            item("a", 1, "ALTER TABLE t ADD COLUMN n TEXT;"),
            item("a2", 1, "ALTER TABLE t ADD COLUMN m TEXT;"),
        ]
    }
    r = c.post("/batches", json=payload)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_batch"


def test_unterminated_string_is_business_error(client):
    c, tmp_path, main = client
    payload = {
        "expected_version": 0,
        "scripts": [{"version": 1, "description": "d", "sql": "INSERT INTO t VALUES (2, 'oops);"}],
    }
    r = c.post("/databases/a/migrate", json=payload)
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "migration_failed"
    assert body["failed_version"] == 1
    assert c.get("/databases/a/version").json()["current_version"] == 0


def test_journal_marks_unfinished_undecided(tmp_path):
    journal = BatchJournal(tmp_path / "b")
    bid = journal.create_batch({"databases": []})
    journal.set_status(bid, "migrating")
    done = journal.create_batch({"databases": []})
    journal.set_result(done, "succeeded", {"status": "succeeded", "databases": []})

    # 模拟重启：新建 Journal 实例扫描未结束批次
    journal2 = BatchJournal(tmp_path / "b")
    marked = journal2.mark_unfinished_undecided()
    assert marked == [bid]
    assert journal2.get(bid)["status"] == "undecided"
    assert journal2.get(done)["status"] == "succeeded"
    # 幂等
    assert journal2.mark_unfinished_undecided() == []


def test_compensation_incomplete_not_reported_as_full_rollback(tmp_path):
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    make_db(db_a)
    make_db(db_b)
    cfg = tmp_path / "aliases.json"
    cfg.write_text(json.dumps({"aliases": {"a": "a.db", "b": "b.db"}}))

    import app.config

    settings = app.config.load_settings(cfg)
    registry = DatabaseRegistry()
    checkpoints = CheckpointStore(tmp_path / "cps")
    journal = BatchJournal(tmp_path / "batches")
    coordinator = BatchCoordinator(settings, registry, checkpoints, journal)

    # 包装 restore：对 a 库补偿时故意失败
    original_restore = checkpoints.restore

    def flaky_restore(alias, *args, **kwargs):
        if alias == "a":
            raise OSError("simulated restore file failure")
        return original_restore(alias, *args, **kwargs)

    checkpoints.restore = flaky_restore
    request = BatchRequest.model_validate(
        {
            "databases": [
                item("a", 1, "ALTER TABLE t ADD COLUMN n TEXT;"),
                item("b", 1, "INSERT INTO nope VALUES (1);"),
            ]
        }
    )
    status_code, payload = coordinator.execute(request)
    assert status_code == 422
    assert payload["status"] == "compensation_incomplete"
    dbs = {d["alias"]: d for d in payload["databases"]}
    assert dbs["a"]["status"] == "restore_failed"
    assert dbs["a"]["error"] and dbs["a"]["checkpoint_id"]
    assert dbs["b"]["status"] == "failed"
    # 日志中的结果同样不是全部回滚
    assert journal.get(payload["batch_id"])["status"] == "compensation_incomplete"
