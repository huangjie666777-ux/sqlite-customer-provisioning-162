"""迁移清单的数据模型与静态校验。"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field, field_validator

from .config import MAX_SCRIPTS


class MigrationItem(BaseModel):
    version: int = Field(ge=1)
    description: str
    sql: str

    @field_validator("description")
    @classmethod
    def _nonblank_desc(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("description must not be blank")
        return value

    @field_validator("sql")
    @classmethod
    def _nonblank_sql(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sql must not be blank")
        return value


class MigrationManifest(BaseModel):
    expected_version: int = Field(ge=0)
    scripts: list[MigrationItem]

    @field_validator("scripts")
    @classmethod
    def _validate_chain(cls, items: list[MigrationItem]) -> list[MigrationItem]:
        if not items:
            raise ValueError("scripts must not be empty")
        if len(items) > MAX_SCRIPTS:
            raise ValueError(f"too many scripts: {len(items)} > {MAX_SCRIPTS}")
        seen: set[int] = set()
        for index, item in enumerate(items, start=1):
            if item.version != index:
                raise ValueError(
                    f"scripts must start at 1 and be consecutive; "
                    f"position {index} has version {item.version}"
                )
            if item.version in seen:
                raise ValueError(f"duplicate version: {item.version}")
            seen.add(item.version)
        return items


def sql_digest(sql: str) -> str:
    """原始 SQL 的 UTF-8 字节 SHA256（十六进制）。"""
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()
