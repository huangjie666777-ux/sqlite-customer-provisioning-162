"""FastAPI 入口：提交迁移清单 / 查询当前版本。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError

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

settings = load_settings()
registry = DatabaseRegistry()

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
    else:
        status_code = 400
        code = "migration_error"
    payload = {"detail": str(exc), "code": code}
    if isinstance(exc, ScriptFailed):
        payload["failed_version"] = exc.version
        payload["reason"] = exc.reason
    return JSONResponse(status_code=status_code, content=payload)


def _resolve(alias: str):
    db_path = settings.aliases.get(alias)
    if db_path is None:
        return JSONResponse(
            status_code=404, content={"detail": f"unknown alias: {alias}", "code": "unknown_alias"}
        )
    return db_path


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "aliases": sorted(settings.aliases)}


@app.get("/databases/{alias}/version")
async def get_version(alias: str):
    resolved = _resolve(alias)
    if isinstance(resolved, JSONResponse):
        return resolved
    return status(resolved)


@app.post("/databases/{alias}/migrate")
async def migrate(alias: str, request: Request):
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
