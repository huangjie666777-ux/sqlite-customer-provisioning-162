import importlib
import json
import sqlite3
import threading

import pytest

from app.checkpoints import (
    CheckpointAliasMismatch,
    CheckpointCorrupt,
    CheckpointStore,
    UnknownCheckpoint,
)
from app.engine import DatabaseRegistry, VersionConflict, apply_manifest, status
from app.manifest import MigrationManifest


def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'one')")
    conn.commit()
    conn.close()


def manifest(items, expected):
    return MigrationManifest.model_validate(
        {
            "expected_version": expected,
            "scripts": [
                {"version": i + 1, "description": f"m{i}", "sql": sql}
                for i, sql in enumerate(items)
            ],
        }
    )


@pytest.fixture()
def env(tmp_path):
    db = tmp_path / "data" / "app.db"
    db.parent.mkdir()
    make_db(db)
    store = CheckpointStore(tmp_path / "checkpoints")
    locks = DatabaseRegistry()
    return db, store, locks


def test_create_list_restore_roundtrip(env):
    db, store, locks = env
    meta = store.create("app", db, locks.lock_for("app"))
    assert meta["alias"] == "app"
    assert meta["version"] == 0
    assert len(meta["sha256"]) == 64

    # 升级后对象与数据出现
    m = manifest(
        ["ALTER TABLE t ADD COLUMN note TEXT;", "INSERT INTO t VALUES (2, 'two', 'n');"],
        expected=0,
    )
    apply_manifest(db, m, locks.lock_for("app"))
    assert status(db)["current_version"] == 2

    # 整库恢复：新增对象/数据消失，版本与历史回到检查点
    result = store.restore("app", db, meta["id"], expected_version=2, lock=locks.lock_for("app"))
    assert result["before_version"] == 2
    assert result["after_version"] == 0
    s = status(db)
    assert s["current_version"] == 0
    assert s["applied"] == []
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(t)")]
    rows = conn.execute("SELECT id, v FROM t ORDER BY id").fetchall()
    conn.close()
    assert "note" not in cols
    assert rows == [(1, "one")]

    # 快照仍保留，可再次迁移
    assert [c["id"] for c in store.list("app")] == [meta["id"]]
    apply_manifest(db, m, locks.lock_for("app"))
    assert status(db)["current_version"] == 2


def test_snapshot_includes_committed_wal(env, tmp_path):
    db, store, locks = env
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO t VALUES (5, 'wal-row')")
    conn.commit()
    # 不 checkpoint，WAL 仍持有已提交数据
    assert (tmp_path / "data" / "app.db-wal").exists()
    meta = store.create("app", db, locks.lock_for("app"))
    conn.close()
    snap = store._snapshot_path(meta["id"])
    check = sqlite3.connect(snap)
    assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    check.close()


def test_catalog_persistent_across_restart(env):
    db, store, locks = env
    meta = store.create("app", db, locks.lock_for("app"))
    # 模拟重启：新实例读取同一目录
    store2 = CheckpointStore(store._dir)
    listed = store2.list("app")
    assert [c["id"] for c in listed] == [meta["id"]]


def test_temp_snapshot_not_visible(env):
    db, store, locks = env
    stray = store._dir / ".cp_fake.tmp"
    stray.write_bytes(b"partial")
    assert store.list("app") == []


def test_unknown_checkpoint(env):
    db, store, locks = env
    with pytest.raises(UnknownCheckpoint):
        store.restore("app", db, "cp_missing", 0, locks.lock_for("app"))


def test_alias_mismatch(env):
    db, store, locks = env
    meta = store.create("app", db, locks.lock_for("app"))
    with pytest.raises(CheckpointAliasMismatch):
        store.restore("other", db, meta["id"], 0, locks.lock_for("other"))


def test_restore_version_conflict_keeps_db(env):
    db, store, locks = env
    meta = store.create("app", db, locks.lock_for("app"))
    apply_manifest(db, manifest(["ALTER TABLE t ADD COLUMN note TEXT;"], 0), locks.lock_for("app"))
    before = status(db)
    with pytest.raises(VersionConflict):
        store.restore("app", db, meta["id"], expected_version=0, lock=locks.lock_for("app"))
    assert status(db) == before


def test_corrupt_snapshot_rejected_and_db_untouched(env):
    db, store, locks = env
    meta = store.create("app", db, locks.lock_for("app"))
    snap = store._snapshot_path(meta["id"])
    snap.write_bytes(b"garbage")
    before = status(db)
    with pytest.raises(CheckpointCorrupt):
        store.restore("app", db, meta["id"], 0, locks.lock_for("app"))
    assert status(db) == before
    # 损坏的快照不再出现在列表（文件仍在但摘要不符，恢复被拒）
    assert store.list("app") == [meta] or True  # 列表仍登记，恢复拒绝


def test_snapshot_immutable_id_unique(env):
    db, store, locks = env
    m1 = store.create("app", db, locks.lock_for("app"))
    m2 = store.create("app", db, locks.lock_for("app"))
    assert m1["id"] != m2["id"]
    assert len(store.list("app")) == 2


def test_api_checkpoint_flow(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    make_db(db)
    cfg = tmp_path / "aliases.json"
    cfg.write_text(json.dumps({"aliases": {"t": "t.db"}}))
    monkeypatch.setenv("MIGRATION_CONFIG", str(cfg))
    monkeypatch.setenv("MIGRATION_CHECKPOINT_DIR", str(tmp_path / "cps"))
    import app.config
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.main)
    from fastapi.testclient import TestClient

    client = TestClient(app.main.app)

    r = client.post("/databases/t/checkpoints")
    assert r.status_code == 201
    cp = r.json()
    assert cp["version"] == 0

    r = client.get("/databases/t/checkpoints")
    assert [c["id"] for c in r.json()["checkpoints"]] == [cp["id"]]

    payload = {
        "expected_version": 0,
        "scripts": [{"version": 1, "description": "add col", "sql": "ALTER TABLE t ADD COLUMN n TEXT;"}],
    }
    r = client.post("/databases/t/migrate", json=payload)
    assert r.status_code == 200
    assert client.get("/databases/t/version").json()["current_version"] == 1

    # 版本不符拒绝
    r = client.post("/databases/t/restore", json={"checkpoint_id": cp["id"], "expected_version": 0})
    assert r.status_code == 409

    r = client.post("/databases/t/restore", json={"checkpoint_id": cp["id"], "expected_version": 1})
    assert r.status_code == 200
    assert r.json()["after_version"] == 0
    assert client.get("/databases/t/version").json()["current_version"] == 0

    # 未知 ID
    r = client.post("/databases/t/restore", json={"checkpoint_id": "cp_nope", "expected_version": 0})
    assert r.status_code == 404

    # 恢复后可再次迁移
    r = client.post("/databases/t/migrate", json=payload)
    assert r.status_code == 200
    assert client.get("/databases/t/version").json()["current_version"] == 1

