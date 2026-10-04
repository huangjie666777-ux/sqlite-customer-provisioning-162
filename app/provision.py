"""新客户数据库开通：从成功发布单的固定清单初始化独立空库。

开通请求只携带发布单 ID、源别名、新别名与幂等键；SQL 与宿主路径
一律取自服务端仓储与配置，不接受客户端提交。新库文件在服务端配置的
开通根目录内以排他方式创建，复用迁移引擎从版本 0 执行发布单中该
源别名的完整清单；初始化与迁移历史提交成功后才把新别名注册进路由。

开通记录持久化在应用库之外的独立 SQLite（provision_dir/provisions.db），
重启后已成功的别名自动恢复路由，未完成的记录标为 undecided，
不开放、不重放 SQL。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .engine import DatabaseRegistry, apply_manifest
from .manifest import MigrationManifest
from .review import ReviewStore


class ProvisionError(Exception):
    """开通流程业务错误。"""

    def __init__(self, detail: str, code: str, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.status_code = status_code


class UnknownProvision(ProvisionError):
    def __init__(self, provision_id: str) -> None:
        super().__init__(
            f"unknown provision: {provision_id}", "unknown_provision", 404
        )


class ProvisionRequest(BaseModel):
    # 只接受这四个字段；新的 SQL、宿主路径等一律拒绝。
    model_config = ConfigDict(extra="forbid")

    release_id: str = Field(min_length=1)
    source_alias: str = Field(min_length=1)
    new_alias: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
    )
    idempotency_key: str = Field(min_length=1, max_length=128)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_fingerprint(request: ProvisionRequest) -> str:
    canonical = json.dumps(
        {
            "release_id": request.release_id,
            "source_alias": request.source_alias,
            "new_alias": request.new_alias,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AliasRegistry:
    """静态配置别名 + 开通成功的动态别名，供所有路由统一解析。"""

    def __init__(self, static_aliases: dict[str, Path]) -> None:
        self._static = dict(static_aliases)
        self._dynamic: dict[str, Path] = {}
        self._lock = threading.Lock()

    def get(self, alias: str) -> Path | None:
        with self._lock:
            path = self._dynamic.get(alias)
            if path is not None:
                return path
            return self._static.get(alias)

    def all(self) -> dict[str, Path]:
        with self._lock:
            merged = dict(self._static)
            merged.update(self._dynamic)
            return merged

    def add(self, alias: str, path: Path) -> None:
        with self._lock:
            if alias in self._static or alias in self._dynamic:
                raise ProvisionError(
                    f"alias already exists: {alias}", "alias_conflict", 409
                )
            self._dynamic[alias] = path


class ProvisionStore:
    """开通记录仓储与状态流，位于应用数据库之外。"""

    def __init__(
        self,
        provision_dir: Path,
        provision_root: Path,
        reviews: ReviewStore,
        aliases: AliasRegistry,
        db_registry: DatabaseRegistry,
    ) -> None:
        self._dir = Path(provision_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._db = self._dir / "provisions.db"
        self._root = Path(provision_root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._reviews = reviews
        self._aliases = aliases
        self._db_registry = db_registry
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS provisions (
                    provision_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    content_sha256 TEXT NOT NULL,
                    person TEXT NOT NULL,
                    release_id TEXT NOT NULL,
                    source_alias TEXT NOT NULL,
                    source_content_sha256 TEXT NOT NULL,
                    new_alias TEXT NOT NULL UNIQUE,
                    db_path TEXT NOT NULL,
                    target_version INTEGER,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @staticmethod
    def _public(row: sqlite3.Row) -> dict:
        return {
            "provision_id": row["provision_id"],
            "idempotency_key": row["idempotency_key"],
            "person": row["person"],
            "release_id": row["release_id"],
            "source_alias": row["source_alias"],
            "source_content_sha256": row["source_content_sha256"],
            "new_alias": row["new_alias"],
            "target_version": row["target_version"],
            "status": row["status"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row_by_key(self, conn, idempotency_key: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM provisions WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()

    def _target_path(self, new_alias: str) -> Path:
        # 别名已限定为 [A-Za-z0-9_-]，拼接后仍解析校验，杜绝路径越界。
        target = (self._root / f"{new_alias}.db").resolve()
        if target.parent != self._root:
            raise ProvisionError(
                f"alias escapes provision root: {new_alias}", "invalid_alias", 422
            )
        return target

    def provision(self, person: str, request: ProvisionRequest) -> tuple[int, dict]:
        """开通新库，返回 (HTTP 状态码, 响应体)。业务失败不泄漏为 500。"""
        fingerprint = _content_fingerprint(request)
        with self._lock:
            with self._connect() as conn:
                existing = self._row_by_key(conn, request.idempotency_key)
            if existing is not None:
                if existing["content_sha256"] != fingerprint:
                    raise ProvisionError(
                        "idempotency key was used with different content",
                        "idempotency_conflict",
                        409,
                    )
                # 同键同内容：返回同一开通记录及结果，不重放 SQL。
                return 200, self._public(existing)

            if self._aliases.get(request.new_alias) is not None:
                raise ProvisionError(
                    f"alias already exists: {request.new_alias}",
                    "alias_conflict",
                    409,
                )
            target = self._target_path(request.new_alias)
            if target in set(self._aliases.all().values()) or target.exists():
                raise ProvisionError(
                    f"database file already exists for alias: {request.new_alias}",
                    "alias_conflict",
                    409,
                )

            # 来源必须是成功发布的发布单；待审/拒绝/失败/未决一律拒绝。
            release = self._reviews.get(request.release_id)
            if release["status"] != "succeeded":
                raise ProvisionError(
                    f"release is {release['status']}, not succeeded",
                    "invalid_release_state",
                    409,
                )
            entry = next(
                (
                    d
                    for d in release["content"]["databases"]
                    if d["alias"] == request.source_alias
                ),
                None,
            )
            if entry is None:
                raise ProvisionError(
                    f"release {request.release_id} has no alias: {request.source_alias}",
                    "unknown_source_alias",
                    422,
                )
            # 清单完整取自发布单（含原始 SQL 与摘要），从版本 0 初始化。
            manifest = MigrationManifest(expected_version=0, scripts=entry["scripts"])

            provision_id = "prov_%s_%s" % (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                uuid.uuid4().hex[:12],
            )
            now = _now()
            with self._connect() as conn:
                try:
                    conn.execute(
                        "INSERT INTO provisions (provision_id, idempotency_key, "
                        "content_sha256, person, release_id, source_alias, "
                        "source_content_sha256, new_alias, db_path, status, "
                        "created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'provisioning', ?, ?)",
                        (
                            provision_id,
                            request.idempotency_key,
                            fingerprint,
                            person,
                            request.release_id,
                            request.source_alias,
                            release["content_sha256"],
                            request.new_alias,
                            str(target),
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    # 并发开通同别名/同幂等键：最多一个成功。
                    raise ProvisionError(
                        f"alias or idempotency key conflict: {exc}",
                        "alias_conflict",
                        409,
                    ) from exc

        try:
            # 排他创建空库文件，绝不覆盖已有文件。
            fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.close(fd)
        except FileExistsError:
            self._finish(provision_id, "failed", None, "database file already exists")
            raise ProvisionError(
                f"database file already exists: {target.name}", "alias_conflict", 409
            )
        except OSError as exc:
            self._finish(provision_id, "failed", None, str(exc))
            raise ProvisionError(
                f"cannot create database file: {exc}", "provisioning_failed", 422
            )

        try:
            result = apply_manifest(
                target, manifest, self._db_registry.lock_for(request.new_alias)
            )
        except Exception as exc:
            # 失败保留错误，不自动重试；半建库文件不开放路由，直接移除。
            self._finish(provision_id, "failed", None, str(exc))
            try:
                target.unlink()
            except OSError:
                pass
            record = self.get(provision_id)
            record["code"] = "provisioning_failed"
            record["detail"] = str(exc)
            return 422, record

        # 初始化与迁移历史提交成功后才开放新别名。
        self._finish(provision_id, "succeeded", result.after_version, None)
        self._aliases.add(request.new_alias, target)
        return 201, self.get(provision_id)

    def _finish(
        self, provision_id: str, status: str, version: int | None, error: str | None
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE provisions SET status = ?, target_version = ?, error = ?, "
                "updated_at = ? WHERE provision_id = ?",
                (status, version, error, _now(), provision_id),
            )

    def get(self, provision_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM provisions WHERE provision_id = ?", (provision_id,)
            ).fetchone()
        if row is None:
            raise UnknownProvision(provision_id)
        return self._public(row)

    def list(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM provisions ORDER BY created_at, provision_id"
            ).fetchall()
        return [self._public(r) for r in rows]

    def reconcile(self) -> list[str]:
        """重启恢复：成功别名重新注册；未完成记录标为未决，不开放、不重放。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE provisions SET status = 'undecided', "
                "error = COALESCE(error, 'service restarted mid-provisioning'), "
                "updated_at = ? WHERE status = 'provisioning'",
                (_now(),),
            )
            rows = conn.execute(
                "SELECT new_alias, db_path FROM provisions WHERE status = 'succeeded'"
            ).fetchall()
        restored: list[str] = []
        for row in rows:
            path = Path(row["db_path"])
            if not path.exists():
                continue
            try:
                self._aliases.add(row["new_alias"], path)
            except ProvisionError:
                continue
            restored.append(row["new_alias"])
        return restored
