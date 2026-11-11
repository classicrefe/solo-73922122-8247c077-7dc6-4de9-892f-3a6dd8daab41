"""FastAPI application: lab water-sample report publishing service."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import database, service
from .schemas import (
    PreviewRequest,
    PreviewResponse,
    PublishRequest,
    PublishResponse,
    ResultCorrectRequest,
    ResultCreateRequest,
    ResultResponse,
    ReportResponse,
)

_DESCRIPTION = """
## 水样检测报告发布服务

报告员在发布前需要确认预览结果**未被更正**。本服务以 SQLite 持久化已确认结果，
每次更正追加一条新修订（旧修订永不覆盖），并围绕修订号实现并发安全的发布与自动撤回。

### 业务规则

- **结果与修订**：`POST /v1/results` 登记结果（结果 ID、样品码、项目、值，修订号从 1 开始）；
  `POST /v1/results/{result_id}/corrections` 每次更正生成新修订，可通过
  `GET /v1/results/{result_id}` 查看逐条修订历史。
- **预览**：`POST /v1/report-preview` 选择同一水样的 1～30 个结果 ID，返回完整内容及
  每个结果的逐条修订，当前修订号用于发布。
- **发布**：`POST /v1/reports` 携带预览得到的修订号；在写事务内复查，**任一来源已更正则
  整份拒绝**（409 `stale_revision`）；不相关结果的更正不影响发布。
- **不可变与撤回**：已发布内容快照不可变；任一来源再次更正时，所有引用它的**有效**报告
  在同一事务内撤回并记录原因；即使值改回原值，报告也不会恢复有效。
- **条件查询**：`GET /v1/reports/{report_id}` 返回原内容、所用修订及 `valid`/`withdrawn`
  状态，支持 `ETag` / `If-None-Match`。撤回会使 ETag 版本号递增，**撤回必定改变 ETag**，
  不会因正文不变而返回 304。

### 并发与部署

两个 API 进程共享同一个 SQLite（WAL）。所有写操作使用 `BEGIN IMMEDIATE` 获取全局写锁，
更正、撤回、发布的并发执行等价于某种串行顺序；数据存放在 Docker 卷中，重启后保留。

### 推荐试用顺序

1. `POST /v1/results`（重复登记几个同水样结果，可点 Try it out 用示例值）
2. `POST /v1/report-preview` 预览并记录每个结果的 `current_revision`
3. `POST /v1/reports` 携带修订发布，复制响应里的 `etag`
4. `GET /v1/reports/{report_id}` 加请求头 `If-None-Match: <etag>` 验证 304
5. `POST /v1/results/{result_id}/corrections` 更正来源结果
6. 再次条件查询：报告变为 `withdrawn`、ETag 改变、返回 200 并附撤回原因；重新发布旧修订被 409 拒绝
"""

_TAGS = [
    {"name": "结果与修订", "description": "登记结果、更正（产生新修订）、查询修订历史。"},
    {"name": "报告", "description": "预览、发布、条件查询与撤回状态。"},
    {"name": "运维", "description": "健康检查与入口页。"},
]

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Idempotent schema creation; both replicas may run it concurrently.
    database.initialize()
    yield


app = FastAPI(
    title="水质实验室检测报告发布 API",
    version="1.0.0",
    description=_DESCRIPTION,
    openapi_tags=_TAGS,
    contact={"name": "实验室数据平台"},
    lifespan=lifespan,
    swagger_ui_parameters={"tryItOutEnabled": True, "displayRequestDuration": True},
)


# ---------------------------------------------------------------------------
# Entry page & health
# ---------------------------------------------------------------------------

_INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>水质实验室检测报告发布 API</title>
  <style>
    body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; max-width: 900px; margin: 3rem auto; padding: 0 1rem; color: #1f2937; line-height: 1.65; }
    h1 { color: #0f766e; }
    code, pre { background: #f3f4f6; border-radius: 6px; padding: .1rem .3rem; }
    pre { padding: 1rem; overflow-x: auto; }
    a { color: #0f766e; }
  </style>
</head>
<body>
  <h1>水质实验室检测报告发布 API</h1>
  <p>SQLite 持久化结果与修订；更正产生新修订，发布携带修订号，来源更正自动撤回引用报告，支持 ETag 条件查询。</p>
  <ul>
    <li><a href="/docs">Swagger UI（含预览/发布/更正/条件查询示例，可直接试运行）</a></li>
    <li><a href="/redoc">ReDoc</a></li>
    <li><a href="/openapi.json">OpenAPI JSON</a></li>
    <li><a href="/health">健康检查</a></li>
  </ul>
  <h2>curl 端到端示例</h2>
  <pre>
# 1) 登记同一水样的两个结果
curl -X POST http://localhost:8000/v1/results -H 'Content-Type: application/json' \\
  -d '{"result_id":"R-001","sample_code":"W-2026-0001","item":"pH","value":"7.2"}'
curl -X POST http://localhost:8000/v1/results -H 'Content-Type: application/json' \\
  -d '{"result_id":"R-002","sample_code":"W-2026-0001","item":"浊度","value":"1.1"}'

# 2) 预览（得到 current_revision）
curl -X POST http://localhost:8000/v1/report-preview -H 'Content-Type: application/json' \\
  -d '{"sample_code":"W-2026-0001","result_ids":["R-001","R-002"]}'

# 3) 发布（携带修订号）
curl -X POST http://localhost:8000/v1/reports -H 'Content-Type: application/json' \\
  -d '{"sample_code":"W-2026-0001","selections":[{"result_id":"R-001","revision":1},{"result_id":"R-002","revision":1}]}'

# 4) 条件查询（把 &lt;etag&gt; 换成上一步响应头/正文里的 ETag）
curl -i http://localhost:8000/v1/reports/&lt;report_id&gt; -H 'If-None-Match: &lt;etag&gt;'

# 5) 更正来源结果 -> 引用报告自动撤回
curl -X POST http://localhost:8000/v1/results/R-001/corrections -H 'Content-Type: application/json' \\
  -d '{"new_value":"7.4","reason":"仪器复核后更正"}'

# 6) 再带旧 ETag 条件查询：状态 withdrawn、ETag 已变、返回 200
curl -i http://localhost:8000/v1/reports/&lt;report_id&gt; -H 'If-None-Match: &lt;etag&gt;'</pre>
</body>
</html>"""


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Results & revisions
# ---------------------------------------------------------------------------


@app.post(
    "/v1/results",
    response_model=ResultResponse,
    status_code=201,
    tags=["结果与修订"],
    summary="登记检测结果（初始修订号为 1）",
    responses={409: {"description": "结果 ID 已存在"}},
)
def create_result(payload: ResultCreateRequest) -> ResultResponse:
    return service.create_result(
        result_id=payload.result_id,
        sample_code=payload.sample_code,
        item=payload.item,
        value=payload.value,
        reason=payload.reason,
    )


@app.get(
    "/v1/results/{result_id}",
    response_model=ResultResponse,
    tags=["结果与修订"],
    summary="查询结果及其全部修订",
    responses={404: {"description": "结果不存在"}},
)
def get_result(result_id: str) -> ResultResponse:
    return service.get_result(result_id)


@app.post(
    "/v1/results/{result_id}/corrections",
    response_model=ResultResponse,
    tags=["结果与修订"],
    summary="更正结果：追加新修订并撤回引用它的有效报告",
    description=(
        "每次更正把当前修订号 +1 并追加一条修订记录（历史修订保留）。同一写事务内，"
        "所有引用该结果的有效报告被置为 `withdrawn` 并记录撤回原因；即使之后值改回原值，"
        "报告也不会恢复有效。示例请求体："
        '`{"new_value": "7.4", "reason": "仪器复核后更正"}`'
    ),
    responses={404: {"description": "结果不存在"}},
)
def correct_result(result_id: str, payload: ResultCorrectRequest) -> ResultResponse:
    return service.correct_result(result_id, payload.new_value, payload.reason)


# ---------------------------------------------------------------------------
# Report preview / publish / query
# ---------------------------------------------------------------------------


@app.post(
    "/v1/report-preview",
    response_model=PreviewResponse,
    tags=["报告"],
    summary="预览报告：1～30 个同水样结果，含完整内容与逐条修订",
    description=(
        '示例请求体：`{"sample_code": "W-2026-0001", "result_ids": ["R-001", "R-002"]}`。'
        "响应中每个条目的 `current_revision` 即发布时需要携带的修订号，`all_revisions` "
        "为该结果的逐条修订历史。"
    ),
    responses={
        404: {"description": "存在不存在的结果 ID"},
        409: {"description": "结果不属于请求的样品码"},
    },
)
def preview_report(payload: PreviewRequest) -> PreviewResponse:
    return service.preview_report(payload.sample_code, payload.result_ids)


@app.post(
    "/v1/reports",
    response_model=PublishResponse,
    status_code=201,
    tags=["报告"],
    summary="发布报告：携带修订号，任一来源已更正则整份拒绝",
    description=(
        "示例请求体："
        '`{"sample_code": "W-2026-0001", "selections": '
        '[{"result_id": "R-001", "revision": 1}, {"result_id": "R-002", "revision": 1}]}`。'
        "服务在写事务内复查每个修订号：只要一个来源的当前修订与提交值不一致，"
        "返回 409 `stale_revision` 且不写入任何报告；与本报告无关的结果更正不影响发布。"
    ),
    responses={
        404: {"description": "存在不存在的结果 ID"},
        409: {"description": "样品码不匹配，或携带的修订已过期（来源已更正）"},
    },
)
def publish_report(payload: PublishRequest) -> PublishResponse:
    return service.publish_report(payload.sample_code, payload.selections)


@app.get(
    "/v1/reports",
    response_model=list[ReportResponse],
    tags=["报告"],
    summary="列出报告（可按状态过滤）",
)
def list_reports(
    status: str | None = Query(
        default=None,
        pattern="^(valid|withdrawn)$",
        description="仅返回 valid（有效）或 withdrawn（已撤回）的报告。",
    ),
) -> list[ReportResponse]:
    return service.list_reports(status)


def _if_none_match_matches(header_value: str | None, etag: str) -> bool:
    """Evaluate an If-None-Match header against a strong ETag (RFC 7232)."""

    if not header_value:
        return False
    candidates = {token.strip() for token in header_value.split(",") if token.strip()}
    if "*" in candidates:
        return True
    return etag in candidates


@app.get(
    "/v1/reports/{report_id}",
    response_model=ReportResponse,
    tags=["报告"],
    summary="查询报告（支持 ETag / If-None-Match 条件请求）",
    description=(
        "返回发布时的原始内容快照、每条记录所用修订以及 `valid`/`withdrawn` 状态与撤回原因。"
        "响应头始终带当前 `ETag`；若请求头 `If-None-Match` 命中当前 ETag 则返回 **304 Not Modified**。"
        "撤回会递增 ETag 版本，因此撤回后即使正文不变也不会返回 304。"
        "条件查询示例请求头：`If-None-Match: \"abc123...\"`（可在 Swagger 下方 Parameters 中填写）。"
    ),
    responses={
        304: {"description": "If-None-Match 命中当前 ETag，正文未变化"},
        404: {"description": "报告不存在"},
    },
)
def get_report(
    report_id: str,
    if_none_match: str | None = Header(
        default=None,
        description='条件请求头，例如 "5f8a2c..."（强 ETag，含双引号），也支持 * 与逗号列表。',
    ),
) -> Response:
    report, etag = service.get_report(report_id)
    if _if_none_match_matches(if_none_match, etag):
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(
        content=report.model_dump(),
        headers={"ETag": etag, "Cache-Control": "must-revalidate"},
    )
