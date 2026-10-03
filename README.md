# SQLite 版本迁移后端

基于 FastAPI + Python 3.10 标准库 `sqlite3` 的结构/数据迁移服务，供部署程序升级**已有的应用 SQLite 库**。HTTP 调用方只提交库别名、完整迁移清单和预期版本；真实文件路径只在服务端 `aliases.json` 中配置。

## 功能范围

- **别名映射**：`aliases.json` 把别名映射到库文件，HTTP 不接受路径。
- **清单校验**：每项含正整数 `version`、非空 `description`、非空 `sql`；版本必须从 1 连续递增，重复版本、空脚本拒绝；脚本数与请求体大小受限。
- **历史指纹**：库内 `__schema_migration_log__` 表保存版本、说明和原始 SQL 的 UTF-8 字节 SHA256。每次提交必须携带**完整已应用历史**且摘要一致，遗漏或改写一律拒绝；只执行未应用后缀，全部已应用时直接成功且不重执行。
- **单事务原子性**：未应用后缀的结构变更、数据变更和迁移记录在同一个 `BEGIN IMMEDIATE` 事务中提交，任何一条脚本失败整批回滚，响应返回 `failed_version` 与原因。
- **SQL 词法切分**（`app/sqlsplit.py`）：理解单/双引号、反引号、`[方括号]`、行/块注释（可嵌套）、括号深度以及 `CREATE TRIGGER ... BEGIN ... END` 体，正确处理字符串/注释中的分号和含多条语句的触发器体。
- **执行保护**（`app/guard.py`）：保护基于 SQLite 授权回调（`set_authorizer`），不是关键词文本搜索。禁止脚本使用事务控制（`BEGIN/COMMIT/ROLLBACK/SAVEPOINT/RELEASE/END`）、`ATTACH/DETACH`、`PRAGMA`、`VACUUM`、非白名单函数（含 `load_extension`，扩展加载被阻断），禁止读写迁移记录表（包括在迁移记录表上建触发器等间接途径），限制只能访问主库。
- **外键约束**：连接强制 `PRAGMA foreign_keys = ON`，每条脚本执行后做 `PRAGMA foreign_key_check`，违规则回滚整批。
- **并发保护**：进程内每个别名一把锁串行化“版本检查 + 执行”；数据库层使用 `BEGIN IMMEDIATE` 与 `busy_timeout` 协调跨进程写锁。并发提交不会重复应用或绕过预期版本；忙（`database is locked`）返回 503，预期版本不匹配返回 409。
- **重启可查**：版本状态全部来自库内真实记录，服务重启后直接读取。
- **短连接**：每次请求使用独立连接并在结束后关闭（`contextlib.closing`）。

## 目录结构

```
app/
  config.py      别名配置与限制常量
  manifest.py    清单模型与 SHA256
  sqlsplit.py    SQL 词法切分/首关键字
  guard.py       语句层禁止项 + SQLite 授权回调
  engine.py      版本读取、历史核对、事务化应用
  main.py        FastAPI 路由
scripts/make_example_db.py  生成示例库
examples/      演示用清单
tests/         pytest 测试（27 项）
aliases.json   别名 -> 库文件映射（路径相对于该文件）
```

## API

- `GET  /health`
- `GET  /databases/{alias}/version` — 当前版本和每条已应用记录（版本/说明/摘要）
- `POST /databases/{alias}/migrate` — 提交完整清单

请求体：

```json
{
  "expected_version": 0,
  "scripts": [
    {"version": 1, "description": "...", "sql": "ALTER TABLE ...; UPDATE ...;"}
  ]
}
```

错误码：`invalid_manifest`(422)、`history_mismatch`(422)、`migration_failed`(422，含 `failed_version`/`reason`)、`version_conflict`(409)、`database_busy`(503)、`unknown_alias`(404)、请求体超限 413。

## 运行

```bash
# 1. 生成示例库 data/demo.db 与 data/billing.db
.venv/bin/python scripts/make_example_db.py

# 2. 启动
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8011

# 3. 查询版本
curl -s http://127.0.0.1:8011/databases/demo/version

# 4. 成功升级（v1 加列 + v2 多语句触发器）
curl -s -X POST http://127.0.0.1:8011/databases/demo/migrate \
  -H 'Content-Type: application/json' --data @examples/manifests_demo.json

# 5. 幂等重放（expected_version=2）
curl -s -X POST http://127.0.0.1:8011/databases/demo/migrate \
  -H 'Content-Type: application/json' --data @examples/manifests_demo_resend.json

# 6. 失败回滚演示（v1 成功、v2 外键违反 -> 整批回滚）
curl -s -X POST http://127.0.0.1:8011/databases/billing/migrate \
  -H 'Content-Type: application/json' --data @examples/manifests_billing_fail.json

# 7. 篡改迁移记录表被拒
curl -s -X POST http://127.0.0.1:8011/databases/billing/migrate \
  -H 'Content-Type: application/json' --data @examples/manifests_forbidden.json
```

## 测试

```bash
.venv/bin/python -m pytest -q
```

覆盖：词法切分（字符串/注释分号、触发器多语句、CASE..END）、历史摘要核对、遗漏/改写拒绝、预期版本冲突、整批回滚、外键违反回滚、禁止语句与禁止函数、迁移记录表写入/建触发器拒绝、请求体限制、重启读取真实记录。

## 可调环境变量

- `MIGRATION_CONFIG`（默认 `aliases.json`）
- `MIGRATION_MAX_REQUEST_BYTES`（默认 2 MiB）
- `MIGRATION_MAX_SCRIPTS`（默认 200）
- `MIGRATION_SQLITE_TIMEOUT`（busy_timeout，默认 5 秒）

## 说明与边界

- 授权回调在 SQLite 解析期工作，能同时约束普通语句和触发器体引用；内部执行的 `foreign_key_check` 与迁移记录写入通过临时切换全放行回调完成。
- 函数采用白名单以阻断 `load_extension`；若脚本需要其它内建函数，在 `app/guard.py` 的 `ALLOWED_FUNCTIONS` 中登记。
- 迁移脚本面向应用表的 DDL/DML；纯 `SELECT` 无副作用，被拒绝。
- 进程内锁只覆盖单进程；多进程部署时靠 `BEGIN IMMEDIATE` 与 busy_timeout 保证跨进程串行，冲突时调用方应按 409/503 重试。
