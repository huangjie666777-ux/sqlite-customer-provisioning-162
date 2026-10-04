"""FastAPI 入口：提交迁移清单 / 查询当前版本 / 检查点与整库恢复 / 多库批次发布。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from .checkpoints import (
    CheckpointAliasMismatch,
    CheckpointCorrupt,
    CheckpointStore,
    UnknownCheckpoint,
)
from .batch import BatchCoordinator, BatchJournal, BatchRequest, UnknownBatch
from .config import MAX_REQUEST_BYTES, load_settings
from .engine import (
    DatabaseBusy,
    DatabaseRegistry,
    HistoryMismatch,
    MigrationError,
    ScriptFailed,
    VersionConflict,
    apply_manifest,
    status,
)
from .manifest import MigrationManifest
from .provision import (
    AliasRegistry,
    ProvisionError,
    ProvisionRequest,
    ProvisionStore,
)
from .review import DecisionRequest, ReviewError, ReviewStore

settings = load_settings()
registry = DatabaseRegistry()
alias_registry = AliasRegistry(settings.aliases)
checkpoints = CheckpointStore(settings.checkpoint_dir)
batch_journal = BatchJournal(settings.batch_dir)
batches = BatchCoordinator(
    settings, registry, checkpoints, batch_journal, alias_lookup=alias_registry.all
)
reviews = ReviewStore(
    settings.review_dir,
    settings.review_credentials,
    batch_journal,
    batches,
)
provisions = ProvisionStore(
    settings.provision_dir,
    settings.provision_root,
    reviews,
    alias_registry,
    registry,
)
# 重启后发现未结束批次：标为未决，不自动重放 SQL、不宣称成功。
batch_journal.mark_unfinished_undecided()
reviews.reconcile_unfinished()
# 重启恢复已成功开通的别名；未完成的开通记录标为未决，不开放、不重放。
provisions.reconcile()

app = FastAPI(title="SQLite Migration Backend", version="1.0.0")


@app.middleware("http")
async def limit_body(request: Request, call_next):
    declared = request.headers.get("content-length")
    if declared and int(declared) > MAX_REQUEST_BYTES:
        return JSONResponse(
            status_code=413,
            content={"detail": f"request body too large (> {MAX_REQUEST_BYTES} bytes)"},
        )
    response = await call_next(request)
    return response


@app.exception_handler(MigrationError)
async def _migration_error_handler(_: Request, exc: MigrationError) -> JSONResponse:
    if isinstance(exc, VersionConflict):
        status_code = 409
        code = "version_conflict"
    elif isinstance(exc, HistoryMismatch):
        status_code = 422
        code = "history_mismatch"
    elif isinstance(exc, DatabaseBusy):
        status_code = 503
        code = "database_busy"
    elif isinstance(exc, ScriptFailed):
        status_code = 422
        code = "migration_failed"
    elif isinstance(exc, UnknownCheckpoint):
        status_code = 404
        code = "unknown_checkpoint"
    elif isinstance(exc, CheckpointAliasMismatch):
        status_code = 409
        code = "checkpoint_alias_mismatch"
    elif isinstance(exc, CheckpointCorrupt):
        status_code = 422
        code = "checkpoint_corrupt"
    elif isinstance(exc, UnknownBatch):
        status_code = 404
        code = "unknown_batch"
    else:
        status_code = 400
        code = "migration_error"
    payload = {"detail": str(exc), "code": code}
    if isinstance(exc, ScriptFailed):
        payload["failed_version"] = exc.version
        payload["reason"] = exc.reason
    return JSONResponse(status_code=status_code, content=payload)


@app.exception_handler(ReviewError)
async def _review_error_handler(_: Request, exc: ReviewError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code},
    )


@app.exception_handler(ProvisionError)
async def _provision_error_handler(_: Request, exc: ProvisionError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code},
    )


def _review_disabled_response() -> JSONResponse | None:
    if settings.review_enabled:
        return JSONResponse(
            status_code=403,
            content={
                "detail": "review mode is enabled; direct execution is disabled",
                "code": "review_required",
            },
        )
    return None


def _actor(request: Request) -> str:
    return reviews.person_for(request.headers.get("authorization"))


def _invalid_decision(exc: ValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(exc.errors()), "code": "invalid_decision"},
    )


def _resolve(alias: str):
    db_path = alias_registry.get(alias)
    if db_path is None:
        return JSONResponse(
            status_code=404, content={"detail": f"unknown alias: {alias}", "code": "unknown_alias"}
        )
    return db_path


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "aliases": sorted(alias_registry.all()),
        "review_enabled": settings.review_enabled,
    }


@app.get("/databases/{alias}/version")
async def get_version(alias: str):
    resolved = _resolve(alias)
    if isinstance(resolved, JSONResponse):
        return resolved
    return status(resolved)


@app.post("/databases/{alias}/migrate")
async def migrate(alias: str, request: Request):
    blocked = _review_disabled_response()
    if blocked is not None:
        return blocked
    resolved = _resolve(alias)
    if isinstance(resolved, JSONResponse):
        return resolved
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        return JSONResponse(
            status_code=413,
            content={"detail": f"request body too large (> {MAX_REQUEST_BYTES} bytes)"},
        )
    try:
        manifest = MigrationManifest.model_validate_json(raw)
    except ValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(exc.errors()), "code": "invalid_manifest"},
        )
    result = apply_manifest(resolved, manifest, registry.lock_for(alias))
    return {
        "alias": alias,
        "before_version": result.before_version,
        "after_version": result.after_version,
        "applied_versions": result.applied,
        "already_applied": not result.applied,
    }


class RestoreRequest(BaseModel):
    checkpoint_id: str = Field(min_length=1)
    expected_version: int = Field(ge=0)


@app.post("/databases/{alias}/checkpoints", status_code=201)
async def create_checkpoint(alias: str):
    resolved = _resolve(alias)
    if isinstance(resolved, JSONResponse):
        return resolved
    meta = checkpoints.create(alias, resolved, registry.lock_for(alias))
    return meta


@app.get("/databases/{alias}/checkpoints")
async def list_checkpoints(alias: str):
    resolved = _resolve(alias)
    if isinstance(resolved, JSONResponse):
        return resolved
    return {"alias": alias, "checkpoints": checkpoints.list(alias)}


@app.post("/databases/{alias}/restore")
async def restore_checkpoint(alias: str, request: Request):
    blocked = _review_disabled_response()
    if blocked is not None:
        return blocked
    resolved = _resolve(alias)
    if isinstance(resolved, JSONResponse):
        return resolved
    raw = await request.body()
    try:
        payload = RestoreRequest.model_validate_json(raw)
    except ValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(exc.errors()), "code": "invalid_restore_request"},
        )
    result = checkpoints.restore(
        alias,
        resolved,
        payload.checkpoint_id,
        payload.expected_version,
        registry.lock_for(alias),
    )
    return {
        "alias": alias,
        "checkpoint_id": result["checkpoint"]["id"],
        "before_version": result["before_version"],
        "after_version": result["after_version"],
    }


@app.post("/batches")
async def submit_batch(request: Request):
    """多库关联发布：统一准备、按序迁移、失败逆序补偿。"""
    blocked = _review_disabled_response()
    if blocked is not None:
        return blocked
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        return JSONResponse(
            status_code=413,
            content={"detail": f"request body too large (> {MAX_REQUEST_BYTES} bytes)"},
        )
    try:
        batch_request = BatchRequest.model_validate_json(raw)
    except ValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(exc.errors()), "code": "invalid_batch"},
        )
    status_code, payload = batches.execute(batch_request)
    return JSONResponse(status_code=status_code, content=payload)


@app.get("/batches")
async def list_batches() -> dict:
    return {"batches": batch_journal.list()}


@app.get("/batches/{batch_id}")
async def get_batch(batch_id: str):
    detail = batch_journal.get(batch_id)
    if detail is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"unknown batch: {batch_id}", "code": "unknown_batch"},
        )
    return detail


@app.post("/releases", status_code=201)
async def create_release(request: Request):
    actor = _actor(request)
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        return JSONResponse(
            status_code=413,
            content={"detail": f"request body too large (> {MAX_REQUEST_BYTES} bytes)"},
        )
    try:
        batch_request = BatchRequest.model_validate_json(raw)
    except ValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(exc.errors()), "code": "invalid_release"},
        )
    validated = batches.validate_request(batch_request)
    if isinstance(validated, tuple):
        return JSONResponse(status_code=validated[0], content=validated[1])
    return reviews.create(batch_request, actor)


@app.get("/releases")
async def list_releases(request: Request):
    _actor(request)
    return {"releases": reviews.list()}


@app.get("/releases/{release_id}")
async def get_release(release_id: str, request: Request):
    _actor(request)
    return reviews.get(release_id)


@app.post("/releases/{release_id}/approve")
async def approve_release(release_id: str, request: Request):
    actor = _actor(request)
    try:
        payload = DecisionRequest.model_validate_json(await request.body())
    except ValidationError as exc:
        return _invalid_decision(exc)
    return reviews.decide(release_id, actor, True, payload.content_sha256)


@app.post("/releases/{release_id}/reject")
async def reject_release(release_id: str, request: Request):
    actor = _actor(request)
    try:
        payload = DecisionRequest.model_validate_json(await request.body())
    except ValidationError as exc:
        return _invalid_decision(exc)
    return reviews.decide(release_id, actor, False, payload.content_sha256)


@app.post("/releases/{release_id}/cancel")
async def cancel_release(release_id: str, request: Request):
    actor = _actor(request)
    return reviews.cancel(release_id, actor)


@app.post("/releases/{release_id}/execute")
async def execute_release(release_id: str, request: Request):
    # 执行触发不接受方案或署名；只允许路径中的发布单 ID。
    _actor(request)
    result = reviews.execute(release_id)
    status_code = int(result.pop("http_status_code", 0) or 0)
    if status_code == 0:
        batch_result = result.get("result") or {}
        batch_status = batch_result.get("status")
        code = batch_result.get("code")
        if batch_status == "succeeded":
            status_code = 200
        elif code == "version_conflict":
            status_code = 409
        elif code == "database_busy":
            status_code = 503
        elif batch_status in {"execution_failed", "undecided"}:
            status_code = 500
        else:
            status_code = 422
    return JSONResponse(status_code=status_code, content=result)


@app.post("/provisions")
async def create_provision(request: Request):
    """新客户数据库开通：凭成功发布单的固定清单初始化独立空库。"""
    actor = _actor(request)
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        return JSONResponse(
            status_code=413,
            content={"detail": f"request body too large (> {MAX_REQUEST_BYTES} bytes)"},
        )
    try:
        payload = ProvisionRequest.model_validate_json(raw)
    except ValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(exc.errors()), "code": "invalid_provision"},
        )
    status_code, body = provisions.provision(actor, payload)
    return JSONResponse(status_code=status_code, content=body)


@app.get("/provisions")
async def list_provisions(request: Request) -> dict:
    _actor(request)
    return {"provisions": provisions.list()}


@app.get("/provisions/{provision_id}")
async def get_provision(provision_id: str, request: Request):
    _actor(request)
    return provisions.get(provision_id)
