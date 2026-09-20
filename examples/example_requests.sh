#!/usr/bin/env bash
# 实验室样本冻融追踪 API —— 端到端示例请求
# 用法：
#   ./examples/example_requests.sh                 # 默认 http://localhost:8000
#   BASE_URL=http://127.0.0.1:8000 ./examples/example_requests.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SID="SAMPLE-DEMO"

c() { printf '\n\033[36m### %s\033[0m\n' "$*"; }

c "1) 健康检查（启动即建库）"
curl -sS "$BASE_URL/health" | python3 -m json.tool

c "2) 登记样本（自定义阈值：-20°C 冻结 / 0°C 解冻，滞回带 (-20, 0)；循环上限 1）"
curl -sS -X POST "$BASE_URL/samples" \
  -H 'Content-Type: application/json' \
  -d "{
    \"id\": \"$SID\",
    \"name\": \"演示样本\",
    \"freeze_threshold\": -20.0,
    \"thaw_threshold\": 0.0,
    \"initial_state\": \"thawed\",
    \"min_allowed_temp\": -80.0,
    \"max_allowed_temp\": 10.0,
    \"max_freeze_thaw_cycles\": 1
  }" | python3 -m json.tool

c "3) 提交一批读数（乱序提交，服务端按 event_time 重排）"
# 按事件时间顺序：+4°C -> -22°C(冻结) -> +3°C(解冻，完成第1循环) -> -21°C(再次冻结)
curl -sS -X POST "$BASE_URL/samples/$SID/readings" \
  -H 'Content-Type: application/json' \
  -d '{"readings": [
    {"event_time": "2026-09-20T12:00:00Z", "temperature_c": 3.0},
    {"event_time": "2026-09-20T09:00:00Z", "temperature_c": 4.0},
    {"event_time": "2026-09-20T12:05:00Z", "temperature_c": -21.0},
    {"event_time": "2026-09-20T09:05:00Z", "temperature_c": -22.5}
  ]}' | python3 -m json.tool

c "4) 查询当前状态与循环次数"
curl -sS "$BASE_URL/samples/$SID/status" | python3 -m json.tool

c "5) 查询完整冻融循环"
curl -sS "$BASE_URL/samples/$SID/cycles" | python3 -m json.tool

c "6) 查询状态翻转记录"
curl -sS "$BASE_URL/samples/$SID/transitions" | python3 -m json.tool

c "7) 幂等重试：重复提交 09:05 的读数 + 复用幂等键，应被去重 (status=duplicate)"
curl -sS -X POST "$BASE_URL/samples/$SID/readings" \
  -H 'Content-Type: application/json' \
  -d '{"readings": [
    {"event_time": "2026-09-20T09:05:00Z", "temperature_c": -22.5},
    {"event_time": "2026-09-20T15:00:00Z", "temperature_c": -19.9, "idempotency_key": "device-42-0001"},
    {"event_time": "2026-09-20T16:00:00Z", "temperature_c": -19.8, "idempotency_key": "device-42-0001"}
  ]}' | python3 -m json.tool

c "8) 迟到数据补录：补一个更早时间点 -10°C（滞回带内，不应改变任何结论）"
curl -sS -X POST "$BASE_URL/samples/$SID/readings" \
  -H 'Content-Type: application/json' \
  -d '{"readings": [
    {"event_time": "2026-09-20T08:30:00Z", "temperature_c": -10.0}
  ]}' | python3 -m json.tool

c "9) 越限/超循环告警：17:00 解冻完成第 2 个循环（超过上限 1），18:00 温度 -85°C 低于下限 -80°C"
curl -sS -X POST "$BASE_URL/samples/$SID/readings" \
  -H 'Content-Type: application/json' \
  -d '{"readings": [
    {"event_time": "2026-09-20T17:00:00Z", "temperature_c": 2.0},
    {"event_time": "2026-09-20T18:00:00Z", "temperature_c": -85.0}
  ]}' > /dev/null

c "10) 查询全部告警（含温度超限 + 循环次数超限）"
curl -sS "$BASE_URL/alerts?sample_id=$SID" | python3 -m json.tool

c "11) 只看严重告警"
curl -sS "$BASE_URL/alerts?sample_id=$SID&severity=critical" | python3 -m json.tool

echo
echo "演示完成。交互式文档：$BASE_URL/docs"
