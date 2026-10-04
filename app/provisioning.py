"""从成功审核发布单开通独立空客户库。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from .config import Settings
from .engine import (
    DatabaseRegistry,
    MigrationError,
    apply_manifest,
)
from .manifest import MigrationItem, MigrationManifest, sql_digest
from .review import ReviewStore


class ProvisioningError(Exception):
    def __init__(self, detail: str, code: str, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.status_code = status_code


class UnknownProvisioning(ProvisioningError):
    def __init__(self, provisioning_id: str) -> None:
        super().__init__(f"unknown provisioning: {provisioning_id}", "unknown_provisioning", 404)


ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ProvisioningRequest(BaseModel):
    release_id: str = Field(min_length=1)
    source_alias: str = Field(min_length=1)
    new_alias: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_fingerprint(payload: ProvisioningRequest) -> str:
    raw = json.dumps(
        payload.model_dump(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ProvisioningStore:
    def __init__(
        self,
        settings: Settings,
        reviews: ReviewStore,
        registry: DatabaseRegistry,
    ) -> None:
        self._settings = settings
        self._reviews = reviews
        self._registry = registry
        self._dir = settings.provisioning_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._root = settings.provisioning_root
        self._root.mkdir(parents=True, exist_ok=True)
        self._db = self._dir / "provisionings.db"
        self._global_lock = threading.RLock()
        self._condition = threading.Condition(self._global_lock)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS provisionings (
                    provisioning_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    person TEXT NOT NULL,
                    release_id TEXT NOT NULL,
                    source_alias TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    new_alias TEXT NOT NULL UNIQUE,
                    database_path TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    source_summary_json TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_provisioning_idempotency "
                "ON provisionings (idempotency_key)"
            )
        self.recover()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _public(self, row: sqlite3.Row) -> dict:
        return {
            "provisioning_id": row["provisioning_id"],
            "idempotency_key": row["idempotency_key"],
            "person": row["person"],
            "release_id": row["release_id"],
            "source_alias": row["source_alias"],
            "source_digest": row["source_digest"],
            "source_summary": json.loads(row["source_summary_json"]),
            "new_alias": row["new_alias"],
            "version": row["version"],
            "status": row["status"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _source_manifest(self, release: dict, source_alias: str) -> tuple[MigrationManifest, list[dict], int, str]:
        if release["status"] != "succeeded":
            raise ProvisioningError(
                f"release is {release['status']}, not succeeded",
                "invalid_release_state",
                409,
            )
        entries = [d for d in release["content"]["databases"] if d["alias"] == source_alias]
        if len(entries) != 1:
            raise ProvisioningError(
                f"source alias not found in release: {source_alias}",
                "unknown_source_alias",
                404,
            )
        entry = entries[0]
        scripts = [MigrationItem.model_validate(item) for item in entry["scripts"]]
        manifest = MigrationManifest(expected_version=0, scripts=scripts)
        summary = [
            {
                "version": item.version,
                "description": item.description,
                "sql_sha256": sql_digest(item.sql),
            }
            for item in manifest.scripts
        ]
        return manifest, summary, len(manifest.scripts), release["content_sha256"]

    def _safe_path(self, alias: str) -> Path:
        if not ALIAS_RE.fullmatch(alias):
            raise ProvisioningError(f"invalid new alias: {alias}", "invalid_alias", 422)
        candidate = (self._root / f"{alias}.db").resolve()
        root = self._root
        if candidate.parent != root:
            raise ProvisioningError(f"alias escapes provisioning root: {alias}", "invalid_alias", 422)
        return candidate

    def provision(self, payload: ProvisioningRequest, person: str) -> tuple[int, dict]:
        with self._condition:
            while True:
                existing = self._find_idempotent(payload.idempotency_key)
                if existing is not None:
                    if existing["status"] == "processing":
                        self._condition.wait(timeout=0.2)
                        continue
                    self._assert_same_request(existing, payload)
                    status_code = 200 if existing["status"] == "succeeded" else 422
                    return status_code, self._public(existing)
                return self._create_and_run(payload, person)

    def _find_idempotent(self, key: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM provisionings WHERE idempotency_key = ?",
                (key,),
            ).fetchone()

    def _assert_same_request(self, row: sqlite3.Row, payload: ProvisioningRequest) -> None:
        if row["request_sha256"] != request_fingerprint(payload):
            raise ProvisioningError(
                "idempotency key was already submitted with different content",
                "idempotency_conflict",
                409,
            )

    def _create_and_run(self, payload: ProvisioningRequest, person: str) -> tuple[int, dict]:
        if payload.source_alias == payload.new_alias:
            raise ProvisioningError("new alias must differ from source alias", "alias_conflict", 409)
        if payload.new_alias in self._settings.aliases:
            raise ProvisioningError(f"alias already exists: {payload.new_alias}", "alias_conflict", 409)
        db_path = self._safe_path(payload.new_alias)
        if db_path.exists():
            raise ProvisioningError(f"database file already exists: {payload.new_alias}", "alias_conflict", 409)
        release = self._reviews.get(payload.release_id)
        manifest, summary, version, release_digest = self._source_manifest(release, payload.source_alias)
        provisioning_id = "prov_%s_%s" % (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            uuid.uuid4().hex[:12],
        )
        now = _now()
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO provisionings (provisioning_id, idempotency_key, person, "
                    "release_id, source_alias, source_digest, new_alias, database_path, "
                    "version, status, request_sha256, source_summary_json, manifest_json, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'processing', ?, ?, ?, ?, ?)",
                    (
                        provisioning_id,
                        payload.idempotency_key,
                        person,
                        payload.release_id,
                        payload.source_alias,
                        release_digest,
                        payload.new_alias,
                        str(db_path),
                        request_fingerprint(payload),
                        json.dumps(summary, ensure_ascii=False),
                        json.dumps(manifest.model_dump(), ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ProvisioningError(str(exc), "alias_conflict", 409) from exc

        status_code = 201
        error = None
        try:
            with open(db_path, "xb"):
                pass
            result = apply_manifest(db_path, manifest, self._registry.lock_for(payload.new_alias))
            if result.after_version != version:
                raise RuntimeError(f"provisioned version {result.after_version} != {version}")
            final_status = "succeeded"
        except Exception as exc:
            final_status = "failed"
            error = str(exc)
            status_code = 422

        with self._condition, self._connect() as conn:
            conn.execute(
                "UPDATE provisionings SET status = ?, version = ?, error = ?, updated_at = ? "
                "WHERE provisioning_id = ?",
                (final_status, version if final_status == "succeeded" else 0, error, _now(), provisioning_id),
            )
            if final_status == "succeeded":
                self._settings.aliases[payload.new_alias] = db_path
            self._condition.notify_all()
            row = conn.execute(
                "SELECT * FROM provisionings WHERE provisioning_id = ?", (provisioning_id,)
            ).fetchone()
        return status_code, self._public(row)

    def recover(self) -> None:
        with self._global_lock, self._connect() as conn:
            conn.execute(
                "UPDATE provisionings SET status = 'undecided', error = COALESCE(error, 'service restarted before provisioning completed'), updated_at = ? "
                "WHERE status = 'processing'",
                (_now(),),
            )
            rows = conn.execute("SELECT * FROM provisionings WHERE status = 'succeeded'").fetchall()
        for row in rows:
            path = Path(row["database_path"]).resolve()
            if path.exists() and path.parent == self._root:
                self._settings.aliases[row["new_alias"]] = path

    def list(self, person: str) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM provisionings ORDER BY created_at, provisioning_id"
            ).fetchall()
        return {"provisionings": [self._public(r) for r in rows]}

    def get(self, provisioning_id: str, person: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM provisionings WHERE provisioning_id = ?", (provisioning_id,)
            ).fetchone()
        if row is None:
            raise UnknownProvisioning(provisioning_id)
        return self._public(row)
