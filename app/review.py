"""双人审核发布单：不可变内容、身份映射与原子状态流。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from .batch import BatchCoordinator, BatchJournal, BatchRequest


class ReviewError(Exception):
    """审核流程业务错误。"""

    def __init__(self, detail: str, code: str, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.status_code = status_code


class UnknownRelease(ReviewError):
    def __init__(self, release_id: str) -> None:
        super().__init__(f"unknown release: {release_id}", "unknown_release", 404)


TERMINAL_RELEASE_STATUSES = frozenset(
    {
        "rejected",
        "revoked",
        "succeeded",
        "prepare_failed",
        "compensated",
        "compensation_incomplete",
        "execution_failed",
        "undecided",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_content(request: BatchRequest) -> bytes:
    return json.dumps(
        request.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def content_hash(request: BatchRequest) -> str:
    return hashlib.sha256(canonical_content(request)).hexdigest()


class DecisionRequest(BaseModel):
    content_sha256: str = Field(min_length=1)


class ReviewStore:
    """发布单与审核决定的持久仓储，位于应用数据库之外。"""

    def __init__(
        self,
        review_dir: Path,
        credentials: dict[str, str],
        journal: BatchJournal,
        coordinator: BatchCoordinator,
    ) -> None:
        self._dir = Path(review_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._db = self._dir / "releases.db"
        self._credentials = dict(credentials)
        self._journal = journal
        self._coordinator = coordinator
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS releases (
                    release_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    author TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    batch_id TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS release_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    release_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    event TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def person_for(self, credential: str | None) -> str:
        if not credential or not credential.startswith("Bearer "):
            raise ReviewError("missing bearer credential", "unauthorized", 401)
        token = credential[len("Bearer ") :].strip()
        person = self._credentials.get(token)
        if person is None:
            raise ReviewError("invalid credential", "unauthorized", 401)
        return person

    def _event(self, conn, release_id: str, actor: str, event: str, payload: dict) -> None:
        conn.execute(
            "INSERT INTO release_events (release_id, ts, actor, event, payload_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (release_id, _now(), actor, event, json.dumps(payload, ensure_ascii=False)),
        )

    def _row(self, conn, release_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM releases WHERE release_id = ?", (release_id,)
        ).fetchone()
        if row is None:
            raise UnknownRelease(release_id)
        return row

    def _public(self, row: sqlite3.Row, events: list[dict] | None = None) -> dict:
        data = {
            "release_id": row["release_id"],
            "status": row["status"],
            "author": row["author"],
            "content_sha256": row["content_sha256"],
            "content": json.loads(row["content_json"]),
            "batch_id": row["batch_id"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if events is not None:
            data["events"] = events
        return data

    def create(self, request: BatchRequest, actor: str) -> dict:
        digest = content_hash(request)
        content = request.model_dump()
        release_id = "rel_%s_%s" % (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            uuid.uuid4().hex[:12],
        )
        with self._lock, self._connect() as conn:
            now = _now()
            conn.execute(
                "INSERT INTO releases (release_id, status, author, content_sha256, "
                "content_json, created_at, updated_at) VALUES (?, 'pending', ?, ?, ?, ?, ?)",
                (
                    release_id,
                    actor,
                    digest,
                    json.dumps(content, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            self._event(conn, release_id, actor, "created", {"content_sha256": digest})
            row = self._row(conn, release_id)
        return self._public(row)

    def get(self, release_id: str) -> dict:
        with self._connect() as conn:
            row = self._row(conn, release_id)
            event_rows = conn.execute(
                "SELECT ts, actor, event, payload_json FROM release_events "
                "WHERE release_id = ? ORDER BY seq",
                (release_id,),
            ).fetchall()
        events = [
            {
                "ts": r["ts"],
                "actor": r["actor"],
                "event": r["event"],
                "payload": json.loads(r["payload_json"]),
            }
            for r in event_rows
        ]
        return self._public(row, events)

    def list(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT release_id, status, author, content_sha256, batch_id, "
                "created_at, updated_at FROM releases ORDER BY created_at, release_id"
            ).fetchall()
        return [dict(r) for r in rows]

    def decide(
        self, release_id: str, actor: str, approve: bool, submitted_digest: str
    ) -> dict:
        new_status = "approved" if approve else "rejected"
        event = "approved" if approve else "rejected"
        with self._lock, self._connect() as conn:
            row = self._row(conn, release_id)
            if row["status"] != "pending":
                raise ReviewError(
                    f"release is {row['status']}, not pending",
                    "invalid_release_state",
                    409,
                )
            if row["author"] == actor:
                raise ReviewError("author cannot review own release", "self_review", 409)
            if row["content_sha256"] != submitted_digest:
                raise ReviewError(
                    "reviewed content digest does not match release",
                    "digest_mismatch",
                    409,
                )
            conn.execute(
                "UPDATE releases SET status = ?, updated_at = ? WHERE release_id = ? AND status = 'pending'",
                (new_status, _now(), release_id),
            )
            self._event(
                conn,
                release_id,
                actor,
                event,
                {"reviewer": actor, "content_sha256": submitted_digest},
            )
            row = self._row(conn, release_id)
        return self._public(row)

    def cancel(self, release_id: str, actor: str) -> dict:
        with self._lock, self._connect() as conn:
            row = self._row(conn, release_id)
            if row["author"] != actor:
                raise ReviewError("only the author can revoke a release", "forbidden", 403)
            if row["status"] not in {"pending", "approved"}:
                raise ReviewError(
                    f"release is {row['status']} and cannot be revoked",
                    "invalid_release_state",
                    409,
                )
            conn.execute(
                "UPDATE releases SET status = 'revoked', updated_at = ? "
                "WHERE release_id = ? AND status IN ('pending', 'approved')",
                (_now(), release_id),
            )
            self._event(conn, release_id, actor, "revoked", {"author": actor})
            row = self._row(conn, release_id)
        return self._public(row)

    def execute(self, release_id: str) -> dict:
        with self._condition:
            while True:
                with self._connect() as conn:
                    row = self._row(conn, release_id)
                    status = row["status"]
                    if status in {"rejected", "revoked", "undecided"}:
                        raise ReviewError(
                            f"release is {status} and cannot be executed",
                            "invalid_release_state",
                            409,
                        )
                    if status in TERMINAL_RELEASE_STATUSES:
                        return self._synchronized_result(release_id)
                    if status == "executing":
                        self._condition.wait(timeout=1.0)
                        continue
                    if status != "approved":
                        raise ReviewError(
                            f"release is {status}, not approved", "invalid_release_state", 409
                        )
                    request = BatchRequest.model_validate(json.loads(row["content_json"]))
                    batch_id = self._journal.reserve_batch_id(
                        {
                            "databases": [
                                {
                                    "alias": d.alias,
                                    "expected_version": d.expected_version,
                                    "script_versions": [s.version for s in d.scripts],
                                }
                                for d in request.databases
                            ],
                            "release_id": release_id,
                        }
                    )
                    changed = conn.execute(
                        "UPDATE releases SET status = 'executing', batch_id = ?, updated_at = ? "
                        "WHERE release_id = ? AND status = 'approved'",
                        (batch_id, _now(), release_id),
                    )
                    if changed.rowcount == 0:
                        continue
                    self._event(
                        conn,
                        release_id,
                        row["author"],
                        "execution_started",
                        {"batch_id": batch_id},
                    )
                    break

        try:
            status_code, batch_result = self._coordinator.execute_reserved(
                request, batch_id
            )
            final_status = str(batch_result.get("status", "execution_failed"))
            if status_code >= 500:
                final_status = "execution_failed"
        except Exception as exc:
            status_code = 500
            final_status = "execution_failed"
            batch_result = {
                "status": "execution_failed",
                "code": "execution_failed",
                "detail": str(exc),
                "batch_id": batch_id,
            }
            try:
                self._journal.set_result(batch_id, "execution_failed", batch_result)
            except Exception:
                pass

        with self._condition, self._connect() as conn:
            conn.execute(
                "UPDATE releases SET status = ?, result_json = ?, updated_at = ? "
                "WHERE release_id = ?",
                (
                    final_status,
                    json.dumps(batch_result, ensure_ascii=False),
                    _now(),
                    release_id,
                ),
            )
            self._event(
                conn,
                release_id,
                "system",
                "execution_finished",
                {"status": final_status, "batch_id": batch_id},
            )
            self._condition.notify_all()
        result = self._synchronized_result(release_id)
        result["http_status_code"] = status_code
        return result

    def _synchronized_result(self, release_id: str) -> dict:
        with self._connect() as conn:
            return self._public(self._row(conn, release_id))

    def reconcile_unfinished(self) -> list[str]:
        """重启恢复：执行中断的发布单与批次均标为未决，不重放 SQL。"""
        marked: list[str] = []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT release_id, batch_id FROM releases WHERE status = 'executing'"
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE releases SET status = 'undecided', updated_at = ? WHERE release_id = ?",
                    (_now(), row["release_id"]),
                )
                self._event(
                    conn,
                    row["release_id"],
                    "system",
                    "undecided",
                    {"batch_id": row["batch_id"], "reason": "service restarted mid-execution"},
                )
                marked.append(row["release_id"])
        for release_id in marked:
            with self._connect() as conn:
                batch_id = conn.execute(
                    "SELECT batch_id FROM releases WHERE release_id = ?", (release_id,)
                ).fetchone()["batch_id"]
            if batch_id:
                self._journal.mark_undecided(batch_id)
        with self._lock, self._connect() as conn:
            known = {
                r["batch_id"]: r["release_id"]
                for r in conn.execute(
                    "SELECT release_id, batch_id FROM releases WHERE batch_id IS NOT NULL"
                ).fetchall()
            }
            for batch_id, release_id in self._journal.undecided_release_links():
                row = conn.execute(
                    "SELECT status FROM releases WHERE release_id = ?", (release_id,)
                ).fetchone()
                if row is None or batch_id in known:
                    continue
                conn.execute(
                    "UPDATE releases SET status = 'undecided', batch_id = ?, updated_at = ? "
                    "WHERE release_id = ? AND status NOT IN ('succeeded', 'undecided')",
                    (batch_id, _now(), release_id),
                )
                self._event(
                    conn,
                    release_id,
                    "system",
                    "undecided",
                    {"batch_id": batch_id, "reason": "service restarted after batch association"},
                )
                marked.append(release_id)
        return marked
