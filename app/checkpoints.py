"""检查点：整库一致性快照的创建、目录持久化与原子恢复。

快照通过 SQLite 在线备份 API 生成（包含已提交的 WAL 数据），
落在应用库之外的 checkpoint_dir 中；目录（catalog.json）与快照文件
持久保存，重启后可查询。历史快照只增不改；临时文件以隐藏名写入，
只有 fsync + 原子 rename 后的完整快照才会出现在列表中。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .config import SQLITE_BUSY_TIMEOUT_SECONDS
from .engine import (
    DatabaseBusy,
    MigrationError,
    VersionConflict,
    _connect,
    _ensure_table,
    _is_busy,
    _read_records,
)


class CheckpointError(MigrationError):
    """检查点业务失败基类。"""


class UnknownCheckpoint(CheckpointError):
    pass


class CheckpointAliasMismatch(CheckpointError):
    pass


class CheckpointCorrupt(CheckpointError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_replace(src: Path, dst: Path) -> None:
    _fsync_file(src)
    os.replace(src, dst)
    dir_fd = os.open(dst.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _integrity_ok(db_file: Path) -> bool:
    uri = f"file:{db_file}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    return len(rows) == 1 and rows[0][0] == "ok"


class CheckpointStore:
    """单进程内的检查点存储；目录与快照都在应用库之外。"""

    def __init__(self, store_dir: Path) -> None:
        self._dir = Path(store_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._catalog_lock = threading.Lock()

    @property
    def _catalog_path(self) -> Path:
        return self._dir / "catalog.json"

    def _snapshot_path(self, checkpoint_id: str) -> Path:
        return self._dir / f"{checkpoint_id}.db"

    def _load_catalog(self) -> list[dict]:
        try:
            raw = json.loads(self._catalog_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, OSError) as exc:
            raise CheckpointCorrupt(f"checkpoint catalog is unreadable: {exc}") from exc
        entries = raw.get("checkpoints", [])
        return [e for e in entries if self._snapshot_path(e.get("id", "")).exists()]

    def _append_catalog(self, meta: dict) -> None:
        with self._catalog_lock:
            try:
                raw = json.loads(self._catalog_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                raw = {"checkpoints": []}
            raw["checkpoints"].append(meta)
            tmp = self._dir / ".catalog.json.tmp"
            tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
            _atomic_replace(tmp, self._catalog_path)

    def list(self, alias: str) -> list[dict]:
        return [e for e in self._load_catalog() if e.get("alias") == alias]

    def create(self, alias: str, db_path: Path, lock: threading.RLock) -> dict:
        """在线备份生成一致快照（含已提交 WAL），原子发布并登记目录。"""
        with lock:
            checkpoint_id = f"cp_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:12]}"
            tmp = self._dir / f".{checkpoint_id}.tmp"
            try:
                with closing(_connect(db_path)) as src:
                    src.execute(
                        "PRAGMA busy_timeout = %d"
                        % int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)
                    )
                    _ensure_table(src)
                    records = _read_records(src)
                    version = records[-1].version if records else 0
                    dst = sqlite3.connect(str(tmp))
                    try:
                        src.backup(dst)
                    finally:
                        dst.close()
            except sqlite3.OperationalError as exc:
                tmp.unlink(missing_ok=True)
                if _is_busy(exc):
                    raise DatabaseBusy(str(exc)) from exc
                raise
            except sqlite3.Error:
                tmp.unlink(missing_ok=True)
                raise
            meta = {
                "id": checkpoint_id,
                "alias": alias,
                "version": version,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "sha256": _sha256_file(tmp),
                "size_bytes": tmp.stat().st_size,
            }
            # 原子发布：rename 前的临时快照对列表不可见。
            _atomic_replace(tmp, self._snapshot_path(checkpoint_id))
            self._append_catalog(meta)
            return meta

    def _get(self, checkpoint_id: str) -> dict:
        for entry in self._load_catalog():
            if entry.get("id") == checkpoint_id:
                return entry
        raise UnknownCheckpoint(f"unknown checkpoint: {checkpoint_id}")

    def restore(
        self,
        alias: str,
        db_path: Path,
        checkpoint_id: str,
        expected_version: int,
        lock: threading.RLock,
    ) -> dict:
        """校验后整库原子恢复；任何失败都不动原库。"""
        with lock:
            meta = self._get(checkpoint_id)
            if meta["alias"] != alias:
                raise CheckpointAliasMismatch(
                    f"checkpoint {checkpoint_id} belongs to alias "
                    f"{meta['alias']!r}, not {alias!r}"
                )
            snapshot = self._snapshot_path(checkpoint_id)
            if not snapshot.exists() or _sha256_file(snapshot) != meta["sha256"]:
                raise CheckpointCorrupt(f"checkpoint {checkpoint_id} digest mismatch")
            if not _integrity_ok(snapshot):
                raise CheckpointCorrupt(f"checkpoint {checkpoint_id} failed integrity_check")

            with closing(_connect(db_path)) as conn:
                conn.execute(
                    "PRAGMA busy_timeout = %d" % int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)
                )
                try:
                    conn.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    if _is_busy(exc):
                        raise DatabaseBusy(str(exc)) from exc
                    raise
                try:
                    _ensure_table(conn)
                    records = _read_records(conn)
                    current = records[-1].version if records else 0
                finally:
                    conn.execute("ROLLBACK")
            if current != expected_version:
                raise VersionConflict(
                    f"expected_version={expected_version} but database is at {current}"
                )

            # 在目标旁的临时文件里重建，校验通过后才原子替换原库。
            tmp = db_path.with_name(f".{db_path.name}.restore-{uuid.uuid4().hex}.tmp")
            try:
                with closing(
                    sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
                ) as src, closing(sqlite3.connect(str(tmp))) as dst:
                    src.backup(dst)
                if not _integrity_ok(tmp):
                    raise CheckpointCorrupt(
                        f"checkpoint {checkpoint_id} restored image failed integrity_check"
                    )
                _atomic_replace(tmp, db_path)
            finally:
                tmp.unlink(missing_ok=True)
            # 清掉旧库的 WAL/SHM 残片，避免新库读到旧日志。
            for suffix in ("-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)
            return {
                "checkpoint": meta,
                "before_version": current,
                "after_version": meta["version"],
            }

