#!/usr/bin/env bash
# End-to-end walk-through against a running stack (default: localhost:8001).
#
# Usage:
#   ./scripts/demo.sh [BASE_URL]
# Example:
#   docker compose up -d --build
#   ./scripts/demo.sh http://localhost:8001
#
# The script covers: registration -> preview -> publish -> conditional GET
# (200 then 304) -> correction -> automatic withdrawal -> ETag change (200,
# never 304 on the old ETag) -> stale revision rejected on re-publish.

set -euo pipefail

BASE_URL="${1:-http://localhost:8001}"
SAMPLE="W-DEMO-$(date +%H%M%S)"
ETAG_FILE="$(mktemp)"
REPORT_FILE="$(mktemp)"
trap 'rm -f "$ETAG_FILE" "$REPORT_FILE"' EXIT

j() { python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=2))'; }
post() { curl -sS -X POST "$BASE_URL$1" -H 'Content-Type: application/json' -d "$2"; }

echo "== 1) 登记同一水样 ${SAMPLE} 的三个结果 =="
for spec in 'R-pH|pH|7.2' 'R-turb|浊度|1.1' 'R-cl|余氯|0.35'; do
  IFS='|' read -r rid item value <<<"$spec"
  post "/v1/results" "{\"result_id\":\"${rid}-${SAMPLE}\",\"sample_code\":\"${SAMPLE}\",\"item\":\"${item}\",\"value\":\"${value}\"}" | j
done

echo
echo "== 2) 预览报告（含逐条修订历史） =="
PREVIEW=$(post "/v1/report-preview" \
  "{\"sample_code\":\"${SAMPLE}\",\"result_ids\":[\"R-pH-${SAMPLE}\",\"R-turb-${SAMPLE}\",\"R-cl-${SAMPLE}\"]}")
echo "$PREVIEW" | j

echo
echo "== 3) 携带预览中的修订号发布 =="
PUBLISHED=$(post "/v1/reports" "{\"sample_code\":\"${SAMPLE}\",\"selections\":["\
"{\"result_id\":\"R-pH-${SAMPLE}\",\"revision\":1},"\
"{\"result_id\":\"R-turb-${SAMPLE}\",\"revision\":1},"\
"{\"result_id\":\"R-cl-${SAMPLE}\",\"revision\":1}]}")
echo "$PUBLISHED" | j
REPORT_ID=$(echo "$PUBLISHED" | python3 -c 'import json,sys; print(json.load(sys.stdin)["report_id"])')

# Capture the strong ETag from a real GET (response header, including quotes).
curl -sS -D - -o "$REPORT_FILE" "$BASE_URL/v1/reports/$REPORT_ID" \
  | tr -d '\r' | awk -F': ' 'tolower($1)=="etag"{print $2}' > "$ETAG_FILE"
ETAG=$(cat "$ETAG_FILE")
echo "当前 ETag: $ETAG"

echo
echo "== 4) 条件查询 If-None-Match 命中 -> 期望 304 =="
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
  "$BASE_URL/v1/reports/$REPORT_ID" -H "If-None-Match: $ETAG"

echo
echo "== 5) 更正来源结果 R-pH -> 引用报告自动撤回 =="
post "/v1/results/R-pH-${SAMPLE}/corrections" \
  '{"new_value":"7.4","reason":"仪器复核后更正"}' | j

echo
echo "== 6) 用旧 ETag 再查 -> 期望 200（撤回必须改变 ETag，不能 304） =="
curl -sS -D - -o "$REPORT_FILE" "$BASE_URL/v1/reports/$REPORT_ID" \
  -H "If-None-Match: $ETAG" | tr -d '\r' | grep -iE '^HTTP|^etag'
echo "报告状态与撤回原因："
j < "$REPORT_FILE"

echo
echo "== 7) 改回原值也不恢复有效，且当前修订变为 3 =="
post "/v1/results/R-pH-${SAMPLE}/corrections" \
  '{"new_value":"7.2","reason":"复测后改回原值"}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision =", d["current_revision"], "| 修订数 =", len(d["revisions"]))'
curl -sS "$BASE_URL/v1/reports/$REPORT_ID" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("报告状态仍为:", d["status"])'

echo
echo "== 8) 携带过期修订重新发布 -> 期望 409 stale_revision =="
post "/v1/reports" "{\"sample_code\":\"${SAMPLE}\",\"selections\":["\
"{\"result_id\":\"R-pH-${SAMPLE}\",\"revision\":1},"\
"{\"result_id\":\"R-turb-${SAMPLE}\",\"revision\":1}]}" | j

echo
echo "== 9) 无关水样的更正不影响本水样发布（新报告，全部用当前修订） =="
post "/v1/results" "{\"result_id\":\"R-OTHER-${SAMPLE}\",\"sample_code\":\"W-OTHER\",\"item\":\"pH\",\"value\":\"8.0\"}" >/dev/null
post "/v1/results/R-OTHER-${SAMPLE}/corrections" '{"new_value":"8.1","reason":"其他样品更正"}' >/dev/null
post "/v1/reports" "{\"sample_code\":\"${SAMPLE}\",\"selections\":["\
"{\"result_id\":\"R-pH-${SAMPLE}\",\"revision\":3},"\
"{\"result_id\":\"R-turb-${SAMPLE}\",\"revision\":1},"\
"{\"result_id\":\"R-cl-${SAMPLE}\",\"revision\":1}]}" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("新报告:", d["report_id"], "| 状态:", d["status"], "| 条目:", d["item_count"])'

echo
echo "演示完成。交互式文档: $BASE_URL/docs"
