# 水质实验室检测报告发布 API

实验室报告员发布水样检测报告前，需要确认预览中的结果没有被更正。本服务提供：

- 检测结果登记与**修订历史**（结果 ID、样品码、项目、值；每次更正追加一条新修订，旧值永不覆盖）；
- 选择同一水样的 **1～30 个结果 ID 预览** JSON 报告（完整内容 + 逐条修订）；
- **携带修订号发布**：事务内复查，任一来源已更正则整份拒绝（409），无关结果的更正不影响发布；
- 已发布内容**不可变快照**；任一来源再次更正，所有引用它的有效报告在同一事务内**自动撤回并记录原因**，即使值改回原值也不恢复有效；
- 报告查询返回原内容、所用修订及 `valid` / `withdrawn` 状态，支持 **ETag / If-None-Match**；撤回必定改变 ETag，不会因正文不变返回 304；
- 两个 API 进程共享一个 SQLite 数据库，更正 / 撤回 / 发布并发时**等价于串行执行**；数据持久化在 Docker 卷中，重启保留。

## 技术栈

FastAPI + Uvicorn + SQLite（WAL 模式），无额外中间件；Pydantic v2 做请求校验；Pytest + httpx 做功能、并发与双进程测试。

## 快速开始

```bash
docker compose up -d --build
# 两个副本：
#   api-1 -> http://localhost:8001
#   api-2 -> http://localhost:8002
# 交互式文档（含预览/发布/更正/条件请求示例，可直接 Try it out）：
#   http://localhost:8001/docs
```

端到端演示（登记 → 预览 → 发布 → 304 → 更正 → 自动撤回 → 409 → 无关更正不影响发布）：

```bash
./scripts/demo.sh http://localhost:8001
```

本地不使用 Docker 运行（需要 Python 3.11）：

```bash
pip install -r requirements.txt
LAB_DB_PATH=./lab_reports.db uvicorn app.main:app --port 8000
# 再以另一个 LAB_DB_PATH 指向同一文件启动第二个进程即可验证双进程行为
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/results` | 登记结果，初始修订号为 1 |
| GET | `/v1/results/{result_id}` | 查询结果与全部修订 |
| POST | `/v1/results/{result_id}/corrections` | 更正：追加新修订，并撤回所有引用该结果的有效报告 |
| POST | `/v1/report-preview` | 同水样 1～30 个结果 ID 预览，含逐条修订 |
| POST | `/v1/reports` | 携带修订号发布；任一来源已更正则 409 整份拒绝 |
| GET | `/v1/reports/{report_id}` | 查询报告，支持 `If-None-Match`（命中返回 304） |
| GET | `/v1/reports?status=valid\|withdrawn` | 列出报告并按状态过滤 |
| GET | `/health` | 健康检查 |

### 典型流程

```bash
# 1. 登记结果
curl -X POST localhost:8001/v1/results -H 'Content-Type: application/json' \
  -d '{"result_id":"R-001","sample_code":"W-0001","item":"pH","value":"7.2"}'

# 2. 预览，记录 current_revision
curl -X POST localhost:8001/v1/report-preview -H 'Content-Type: application/json' \
  -d '{"sample_code":"W-0001","result_ids":["R-001"]}'

# 3. 携带修订发布
curl -X POST localhost:8001/v1/reports -H 'Content-Type: application/json' \
  -d '{"sample_code":"W-0001","selections":[{"result_id":"R-001","revision":1}]}'

# 4. 条件查询（ETag 含双引号，是强校验器）
curl -i localhost:8001/v1/reports/<report_id> -H 'If-None-Match: "<etag>"'

# 5. 更正来源 -> 引用报告自动撤回；旧 ETag 再查返回 200 且 ETag 已变
curl -X POST localhost:8001/v1/results/R-001/corrections \
  -H 'Content-Type: application/json' -d '{"new_value":"7.4","reason":"复核更正"}'
```

错误响应统一为 `{"detail": {"code": "...", "message": "...", "details": ...}}`，关键 code：

- `result_not_found`（404）、`report_not_found`（404）
- `result_already_exists`（409）、`sample_mismatch`（409）
- `stale_revision`（409）：携带的修订号已不是当前修订，整份拒绝，`details.stale_selections` 逐条列出提交值与当前值。

## 数据模型

```text
results(result_id PK, sample_code, item, current_revision, created_at, updated_at)
result_revisions(result_id, revision, value, reason, created_at, PK(result_id, revision))
reports(report_id PK, sample_code, status[valid|withdrawn], etag_version,
        created_at, published_at, withdrawn_at, withdraw_reason)
report_items(report_id, item_index, result_id, sample_code, item, value,
             revision, revision_created_at, PK(report_id, item_index))
```

- `result_revisions` 只追加：更正 = `current_revision + 1` 插入新行，历史修订原样保留。
- `report_items` 是发布时刻的**内容快照**（值与修订号都固化），之后来源如何变化都不改写快照。
- `reports.etag_version` 初始为 1，每次撤回 +1。

## 并发正确性：为什么等价于串行

两个 uvicorn 进程挂载同一个 SQLite 文件（命名卷 `lab-report-data`）。

1. **WAL + busy_timeout**：读写不互斥，写冲突时第二个进程等待而不是立即报 locked。
2. **所有写事务以 `BEGIN IMMEDIATE` 开始**（见 `app/database.py`），立即获取 RESERVED 写锁。SQLite 在 WAL 下同一时刻只允许一个写事务，因此更正、发布、撤回在全库范围被线性化，与某种串行顺序等价。
3. **发布在同一个写事务内复查修订号**：
   - 更正先拿锁 → 发布后拿锁时读到新 `current_revision`，与提交修订不符 → 409 拒绝，绝不产生引用过期修订的有效报告；
   - 发布先拿锁 → 提交修订与当前一致才写入快照；随后更正拿锁时在**同一事务内**把所有引用该结果的有效报告更新为 `withdrawn` 并记录原因。
4. 撤回语句带 `status = 'valid'` 条件并递增 `etag_version`，重复更正不会覆盖首次撤回原因与时间。

测试 `tests/test_concurrency.py` 用多线程（各自独立连接，触发真实锁竞争）验证：30 轮更正/发布竞争中两种串行次序都会出现，且不存在“引用已更正来源却仍有效”的报告；20 个并发更正产生连续的 1..21 修订且无丢失。

## ETag 与撤回

ETag 为强校验器，对 `report_id + status + etag_version + 快照条目` 的规范化 JSON 取 SHA-256（见 `service.compute_etag`）。

- 有效期间快照不可变，ETag 稳定；`If-None-Match` 命中返回 **304**（支持单个 ETag、逗号列表与 `*`）。
- 撤回时 `status` 变为 `withdrawn` 且 `etag_version` +1，ETag 必然改变——即使报告正文一个字节都没变，条件请求也返回 **200** 与撤回原因，不会错误地 304。
- 响应附带 `Cache-Control: must-revalidate`，防止中间缓存复用旧的 304 判定。

## 重启持久化

数据库文件位于命名卷 `/data/lab_reports.db`（WAL 模式另有 `-wal`/`-shm` 旁车文件）。容器重启或重建（卷保留）后，结果、修订、报告快照与撤回状态全部保留。`tests/test_multi_process.py` 启动真实 uvicorn 子进程验证：双进程共享数据、跨进程撤回可见、进程全部退出后新进程读到完全一致的报告与 ETag。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

- `tests/test_api.py`：登记/更正/修订历史、预览校验、发布拒绝、不可变快照、级联撤回、改回原值不恢复、ETag 304/200 循环、撤回必变 ETag。
- `tests/test_concurrency.py`：真实 SQLite 锁竞争下的串行等价性。
- `tests/test_multi_process.py`：两个 uvicorn 进程共享一个数据库文件的端到端行为与重启持久化。

## 目录结构

```text
app/
  config.py     # 数据库路径与常量（LAB_DB_PATH 可覆盖）
  database.py   # WAL 连接、schema、BEGIN IMMEDIATE 事务封装
  schemas.py    # Pydantic 请求/响应模型与 OpenAPI 示例
  service.py    # 业务逻辑：修订、预览、发布复查、撤回、ETag
  main.py       # FastAPI 路由、条件请求、/docs 示例
scripts/demo.sh # 端到端 curl 演示
tests/          # 功能 / 并发 / 双进程测试
```
