"""迁移脚本的执行保护。

保护基于 SQLite 授权回调（``sqlite3.Connection.set_authorizer``）：
SQLite 解析每条语句（包括触发器体内语句）时逐个请求授权，
因此能覆盖“普通语句”和“触发器将来执行时会引用什么对象”两类路径，
不依赖对 SQL 文本的关键词搜索。

另在语句层用真正的词法分析结果拦截事务控制类语句（这些语句
本身不经过授权回调）以及 VACUUM。

"""

from __future__ import annotations

import sqlite3

from .sqlsplit import first_keyword, split_statements

# 明确禁止的语句首关键字（仅匹配词法意义上的第一个裸关键字）。
FORBIDDEN_FIRST_KEYWORDS = frozenset(
    {
        "begin",
        "commit",
        "end",
        "rollback",
        "savepoint",
        "release",
        "attach",
        "detach",
        "pragma",
        "vacuum",
    }
)

# 允许调用的内建函数；其它函数（含 load_extension）默认拒绝。
ALLOWED_FUNCTIONS = frozenset(
    {
        # 常见纯函数白名单，扩展加载函数不在其中。
        "abs", "coalesce", "glob", "ifnull", "instr", "hex", "length",
        "like", "lower", "ltrim", "max", "min", "nullif", "quote",
        "replace", "round", "rtrim", "substr", "trim", "typeof", "upper",
        "unicode", "random", "randomblob", "zeroblob", "date", "time",
        "datetime", "julianday", "strftime", "unixepoch", "printf",
        "format", "concat", "concat_ws", "group_concat", "sum", "total",
        "avg", "count", "distinct", "cast", "json", "json_array",
        "json_object", "json_extract", "json_type", "json_valid",
        "json_insert", "json_replace", "json_set", "json_remove",
        "json_patch", "json_quote", "json_group_array", "json_group_object",
        "json_each", "json_tree", "iif", "likelihood", "likely", "unlikely",
        "btrim", "lpad", "rpad", "unhex", "octet_length", "soundex",
    }
)


class GuardError(ValueError):
    pass


def inspect_script(sql_text: str, migration_table: str) -> list[str]:
    """词法切分并在语句层拒绝事务控制/附加库/VACUUM/PRAGMA 等。"""
    statements = split_statements(sql_text)
    if not statements:
        raise GuardError("blank migration script")
    for stmt in statements:
        head = first_keyword(stmt)
        if head in FORBIDDEN_FIRST_KEYWORDS:
            raise GuardError(f"statement type is forbidden: {head.upper()}")
        if head == "select":
            raise GuardError("read-only SELECT statements are not allowed in migrations")
        # 目标表名检查放在授权器里（ALTER/INSERT 等会在解析期回调）。
        _ = migration_table
    return statements


def make_authorizer(migration_table: str):
    """返回一个严格授权回调，用于迁移脚本执行期间。"""

    table = migration_table

    def authorizer(action, arg1, arg2, db_name, trigger_name):
        # PRAGMA：用户脚本的 PRAGMA 已在语句层拦截；这里只放行引擎内部的
        # foreign_key_check（SQLite 在语句执行期做外键检查时也会回调）。
        if action == sqlite3.SQLITE_PRAGMA:
            if (arg1 or "").lower() == "foreign_key_check":
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        # ATTACH：VACUUM 的内部 attach 文件名为空，用户 ATTACH 必带文件名。
        if action == sqlite3.SQLITE_ATTACH:
            return sqlite3.SQLITE_DENY if arg1 else sqlite3.SQLITE_OK
        # DETACH
        if action == sqlite3.SQLITE_DETACH:
            return sqlite3.SQLITE_DENY
        # 函数调用：load_extension 等非白名单函数一律拒绝。
        if action == sqlite3.SQLITE_FUNCTION:
            fname = (arg2 or "").lower()
            if fname not in ALLOWED_FUNCTIONS:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        # 只能操作主库 main（VACUUM 内部使用 vacuum_db 已在语句层被拦，
        # 此处兜底拒绝其它库名）。
        if db_name not in (None, "main", "temp") and not (
            db_name == "vacuum_db"
        ):
            return sqlite3.SQLITE_DENY
        # 针对迁移记录表的任何访问全部拒绝（读写、建触发器、建索引等）。
        # 注意：ALTER TABLE 等内建操作会读取 main.sqlite_master，属于正常行为。
        obj = (arg1 or "").lower()
        if obj == table.lower():
            return sqlite3.SQLITE_DENY
        # 在迁移记录表上建触发器也会被 CREATE_TRIGGER 回调带出来。
        if action == sqlite3.SQLITE_CREATE_TRIGGER and (arg2 or "").lower() == table.lower():
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorizer
