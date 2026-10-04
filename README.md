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
- **先锁后检**：迁移在 `BEGIN IMMEDIATE` 取得数据库写锁之后才检查预期版本与历史指纹，检查与执行之间不会被其它写入者改写。
- **延迟外键**：外键完整性在整批末尾统一 `PRAGMA foreign_key_check`，允许跨脚本“先破坏后修复”的合法序列；末尾仍违规则整批回滚并返回失败版本。
- **检查点**（`app/checkpoints.py`）：部署前可按别名创建整库一致性快照。快照经 SQLite 在线备份 API 生成，包含应用表、数据、索引、触发器、迁移记录以及已提交的 WAL 数据，不是复制主文件。ID 由服务生成，绑定别名、迁移版本、创建时间和快照 SHA256；目录（`catalog.json`）与快照文件持久保存在应用库之外的 `checkpoint_dir`，重启可查询，历史快照只增不改，临时/不完整快照不出现在列表中。
- **整库恢复**：按 ID 恢复，必须携带预期当前版本且只能恢复同一别名的检查点。恢复前校验快照 SHA256 与 `PRAGMA integrity_check`，在目标旁的临时文件重建并再次校验后原子替换原库；恢复后新增对象与数据消失，版本与历史回到检查点，快照与目录保留，可再次迁移。任何失败（未知 ID、别名不符、版本冲突、快照损坏、库忙）都明确拒绝且原库不变，不留半恢复库。
- **重启可查**：版本状态全部来自库内真实记录，服务重启后直接读取。
- **短连接**：每次请求使用独立连接并在结束后关闭（`contextlib.closing`）。
- **多库关联发布**（`app/batch.py`）：`POST /batches` 提交有序库列表，每项含配置别名、预期版本和完整迁移清单（与单库清单同一套校验）。空列表、重复别名、多个别名映射同一库文件、非法清单一律拒绝；HTTP 不接受宿主路径。协调器先对全部库做版本/历史/脚本检查并逐库创建检查点，**全部准备成功才按输入顺序迁移**；准备失败任何库都不升级。
- **失败补偿**：任一库迁移失败即停止后续库，已升级库按**逆序**恢复到各自检查点（结构、数据、迁移历史一起回退）；失败库保持原状态，未执行库不升级。单个补偿失败仍继续恢复其余库，响应逐库标注 `migrated/restored/restore_failed/failed/not_executed` 并保留错误与检查点 ID，部分补偿记为 `compensation_incomplete`，不会谎报为全部回滚。
- **互斥与单进程**：批次持有一把全局批次锁串行化，再按别名字典序一次性持有全部涉及库的锁，与单库迁移/检查点/恢复互斥；重叠批次不交叉执行，锁顺序一致不会死锁。发布期间应用停写由调用方配合，协调只保证单进程。
- **批次日志**：服务生成批次 ID，计划、每个准备/迁移/补偿步骤与最终结果逐步写入应用库之外的 `batch_dir/journal.db`（独立 SQLite，WAL）。`GET /batches`、`GET /batches/{id}` 可随时查询，重启后历史仍在；重启时发现未结束批次标记为 `undecided`，不自动重放 SQL、不宣称成功。
- **双人审核发布**（`app/review.py`，默认关闭）：开启后，创建发布单必须携带服务端配置的 Bearer 凭据；人员只由凭据映射决定，不接受请求内署名。发布单保存有序库别名、预期版本、完整清单、原始 SQL 与规范化内容 SHA256，一经提交不可修改。另一人批准时必须回传所查看的 SHA256；作者不能审核，摘要不符或并发状态变化均拒绝。作者可在执行开始前撤销；批准/拒绝/撤销/执行通过 SQLite 条件更新和进程内条件变量收敛到一致终态。
- **按单执行与防偷换**：执行请求只包含发布单 ID，协调器从仓储读取批准时的原始方案，不接受请求携带 SQL。执行在原有全局批次锁和库锁内重新预检版本与历史，审批后库已变化则不执行；并发/重复执行最多关联一个批次并返回同一结果。失败终态不自动重跑；服务在执行中断后将发布单与批次标记为 `undecided`，不重放 SQL、不虚报成功，批次关联不丢失。
- **审核模式旁路关闭**：启用审核后，`POST /batches`、单库 `POST /migrate` 与 `POST /restore` 均返回 403；检查点查询和版本读取仍可用于审计。未启用时原接口和行为保持不变。
- **新客户库开通**（`app/provisioning.py`）：持 Bearer 人员凭据提交成功发布单 ID、来源别名、新别名与幂等键。服务端只从成功发布单提取该别名的完整 SQL 清单和摘要，不接收新 SQL 或宿主路径；待审、拒绝、失败、未决发布单均不能作为来源。新库在服务端开通根目录以独占方式创建，从空库版本 0 应用完整清单，不复制源库业务数据，也不修改发布单、批次或源库。
- **开通原子可见性**：开通记录先持久化为 `processing`；库文件创建、全部 SQL 与迁移历史提交成功后才把新别名加入运行时解析。失败保留错误且重复请求返回同一失败记录，不自动重试；重启时未完成记录改为 `undecided`，不开放别名、不重放 SQL。成功记录重启后恢复别名。
- **开通幂等与冲突**：同一幂等键和相同请求返回同一开通记录；换内容返回 409。新别名不得与静态或已开通别名冲突，不允许路径越界，也不覆盖已有文件。成功后的新别名立即支持版本查询、检查点、恢复（非审核模式）及后续审核发布。

## 目录结构

```
app/
  config.py      别名配置与限制常量
  manifest.py    清单模型与 SHA256
  sqlsplit.py    SQL 词法切分/首关键字
  guard.py       语句层禁止项 + SQLite 授权回调
  engine.py      版本读取、历史核对、事务化应用
  checkpoints.py 检查点快照、目录持久化与原子恢复
  batch.py       多库批次协调、批次日志持久化与失败补偿
  review.py      双人审核发布单、凭据身份和审核状态流
  provisioning.py 成功发布单初始化空客户库、开通记录与动态别名
  main.py        FastAPI 路由
scripts/make_example_db.py  生成示例库
examples/      演示用清单
  tests/         pytest 测试（59 项）
aliases.json   别名 -> 库文件映射（路径相对于该文件）
```

## API

- `GET  /health`
- `GET  /databases/{alias}/version` — 当前版本和每条已应用记录（版本/说明/摘要）
- `POST /databases/{alias}/migrate` — 提交完整清单
- `POST /databases/{alias}/checkpoints` — 创建检查点，返回 `id/alias/version/created_at/sha256/size_bytes`
- `GET  /databases/{alias}/checkpoints` — 列出该别名可见检查点
- `POST /databases/{alias}/restore` — 按 ID 整库恢复，请求体 `{"checkpoint_id": "...", "expected_version": N}`
- `POST /batches` — 多库关联发布，请求体 `{"databases": [{"alias", "expected_version", "scripts": [...]}]}`；成功返回每库 `before_version/after_version/checkpoint_id`，失败返回逐库状态与补偿结果
- `GET  /batches` — 列出全部批次（ID、状态、时间）
- `GET  /batches/{batch_id}` — 批次详情：计划、逐步事件、最终结果
- `POST /releases` — 审核模式下创建发布单（Bearer 凭据；请求体同 `/batches`）
- `GET  /releases` / `GET /releases/{release_id}` — 查询发布单、审核事件、批次关联和结果
- `POST /releases/{release_id}/approve` — 他人批准，请求体 `{"content_sha256": "..."}`
- `POST /releases/{release_id}/reject` — 他人拒绝，携带同一内容摘要；作者审核或摘要不符均拒绝
- `POST /releases/{release_id}/cancel` — 作者撤销尚未开始执行的发布单
- `POST /releases/{release_id}/execute` — 仅路径包含发布单 ID 和 Bearer 人员凭据，不接受 SQL 或方案；重复调用返回同一批次结果
- `POST /provisionings` — 从成功发布单开通空客户库，请求体 `{"release_id", "source_alias", "new_alias", "idempotency_key"}`
- `GET /provisionings` / `GET /provisionings/{provisioning_id}` — 使用有效 Bearer 凭据查询开通记录、来源摘要、版本、状态和错误

请求体：

```json
{
  "expected_version": 0,
  "scripts": [
    {"version": 1, "description": "...", "sql": "ALTER TABLE ...; UPDATE ...;"}
  ]
}
```

错误码：`invalid_manifest`(422)、`invalid_batch`(422)、`history_mismatch`(422)、`migration_failed`(422，含 `failed_version`/`reason`，禁用 SQL——包括含未闭合字符串的脚本——也走此码而非 500)、`version_conflict`(409)、`database_busy`(503)、`unknown_alias`(404)、`unknown_checkpoint`(404)、`unknown_batch`(404)、`checkpoint_alias_mismatch`(409)、`checkpoint_corrupt`(422)、审核相关 `unauthorized`(401)、`review_required`(403)、`forbidden`(403)、`self_review`(409)、`digest_mismatch`(409)、`invalid_release_state`(409)、`unknown_release`(404)、开通相关 `invalid_provisioning_request`(422)、`invalid_alias`(422)、`unknown_source_alias`(404)、`alias_conflict`(409)、`idempotency_conflict`(409)、`unknown_provisioning`(404)、请求体超限 413。

审核配置写在 `aliases.json`（凭据应通过部署机密分发，不要提交真实值）：

```json
{
  "review_enabled": true,
  "aliases": {"demo": "data/demo.db", "billing": "data/billing.db"},
  "review_credentials": {
    "change-me-alice": "alice",
    "change-me-bob": "bob"
  }
}
```

开通接口始终需要上述 `review_credentials` 中配置的 Bearer 凭据；即使 `review_enabled=false`，也不会允许匿名开通。新客户库目录单独配置，不与静态库、检查点、批次或审核仓储混用：

```json
{
  "aliases": {"demo": "data/demo.db"},
  "review_credentials": {"change-me-alice": "alice"},
  "provisioning_root": "provisioned",
  "provisioning_dir": "provisioning"
}
```

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

# 8. 部署前创建检查点（返回服务生成的 id）
curl -s -X POST http://127.0.0.1:8011/databases/demo/checkpoints

# 9. 列出检查点
curl -s http://127.0.0.1:8011/databases/demo/checkpoints

# 10. 升级后整库恢复到检查点（expected_version 为当前版本）
curl -s -X POST http://127.0.0.1:8011/databases/demo/restore \
  -H 'Content-Type: application/json' \
  -d `'{"checkpoint_id": "cp_...", "expected_version": 2}'`

# 11. 多库关联发布：demo + billing 一起升级（全部准备成功才迁移）
curl -s -X POST http://127.0.0.1:8011/batches \
  -H 'Content-Type: application/json' --data @examples/batch_success.json

# 12. 失败补偿演示：billing 外键违规，demo 已升级但被逆序恢复
curl -s -X POST http://127.0.0.1:8011/batches \
  -H 'Content-Type: application/json' --data @examples/batch_fail.json

# 13. 查询批次（重启后仍可追溯；未结束批次重启后标为 undecided）
curl -s http://127.0.0.1:8011/batches
curl -s http://127.0.0.1:8011/batches/batch_...

# 14. 双人审核：alice 提交（先用 review_enabled=true 配置重启服务）
REL=$(curl -s -X POST http://127.0.0.1:8011/releases \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer change-me-alice' \
  --data @examples/batch_success.json)
REL_ID=$(printf '%s' "$REL" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["release_id"])')
SHA=$(printf '%s' "$REL" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["content_sha256"])')

# 15. bob 查看后批准自己看到的摘要；alice 本人批准会返回 409
curl -s -H 'Authorization: Bearer change-me-bob' \
  http://127.0.0.1:8011/releases/$REL_ID
curl -s -X POST http://127.0.0.1:8011/releases/$REL_ID/approve \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer change-me-bob' \
  -d "{\"content_sha256\":\"$SHA\"}"

# 16. 执行只提交发布单 ID；重复调用返回同一个 batch_id
curl -s -X POST http://127.0.0.1:8011/releases/$REL_ID/execute \
  -H 'Authorization: Bearer change-me-alice'
curl -s -X POST http://127.0.0.1:8011/releases/$REL_ID/execute \
  -H 'Authorization: Bearer change-me-alice'

# 17. 从成功发布单中的 demo 清单开通空客户库；不提交 SQL 或路径
curl -s -X POST http://127.0.0.1:8011/provisionings \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer change-me-alice' \
  -d '{"release_id":"'"$REL_ID"'","source_alias":"demo","new_alias":"customer_a","idempotency_key":"customer_a_v1"}'

# 18. 新别名立即按原版本接口查询；重复开通请求返回同一记录
curl -s http://127.0.0.1:8011/databases/customer_a/version
curl -s -X POST http://127.0.0.1:8011/provisionings \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer change-me-alice' \
  -d '{"release_id":"'"$REL_ID"'","source_alias":"demo","new_alias":"customer_a","idempotency_key":"customer_a_v1"}'
```

## 测试

```bash
.venv/bin/python -m pytest -q
```

覆盖：词法切分（字符串/注释分号、触发器多语句、CASE..END）、历史摘要核对、遗漏/改写拒绝、预期版本冲突、整批回滚、外键违反回滚、禁止语句与禁止函数、未闭合字符串折算为业务错误、迁移记录表写入/建触发器拒绝、请求体限制、重启读取真实记录、多库批次成功/失败补偿/未执行库/准备失败不升级/校验拒绝/补偿不完全不谎报/文件读写异常不中断其余库恢复/批次日志重启未决标记，以及审核模式旁路关闭、凭据身份、自审/错摘要拒绝、批准后漂移、撤销终态、重复执行同一批次、发布单执行鉴权、发布单持久化、成功发布单开通空库、来源摘要保留、幂等冲突、别名/路径冲突和重启恢复。

## 可调环境变量

- `MIGRATION_CONFIG`（默认 `aliases.json`）
- `MIGRATION_MAX_REQUEST_BYTES`（默认 2 MiB）
- `MIGRATION_MAX_SCRIPTS`（默认 200）
- `MIGRATION_SQLITE_TIMEOUT`（busy_timeout，默认 5 秒）
- `MIGRATION_CHECKPOINT_DIR`（检查点目录，默认 `<配置目录>/checkpoints`，必须在应用库之外）
- `MIGRATION_BATCH_DIR`（批次日志目录，默认 `<配置目录>/batches`，必须在应用库之外）
- `MIGRATION_REVIEW_ENABLED`（`1/true/yes/on` 启用；未设置时读取配置文件 `review_enabled`，默认关闭）
- `MIGRATION_REVIEW_DIR`（审核仓储目录，默认 `<配置目录>/reviews`，必须在应用库之外）
- `MIGRATION_PROVISIONING_ROOT`（新客户库文件根目录，默认 `<配置目录>/provisioned`；新库文件为 `<root>/<安全别名>.db`）
- `MIGRATION_PROVISIONING_DIR`（开通记录 SQLite 仓储目录，默认 `<配置目录>/provisioning`，必须在新客户库和应用库之外）

## 说明与边界

- 授权回调在 SQLite 解析期工作，能同时约束普通语句和触发器体引用；内部执行的 `foreign_key_check` 与迁移记录写入通过临时切换全放行回调完成。
- 函数采用白名单以阻断 `load_extension`；若脚本需要其它内建函数，在 `app/guard.py` 的 `ALLOWED_FUNCTIONS` 中登记。
- 迁移脚本面向应用表的 DDL/DML；纯 `SELECT` 无副作用，被拒绝。
- 进程内锁只覆盖单进程；多进程部署时靠 `BEGIN IMMEDIATE` 与 busy_timeout 保证跨进程串行，冲突时调用方应按 409/503 重试。
