# 实验室样本冻融追踪 API

基于 **FastAPI + SQLite + Docker** 的服务，用于追踪实验室样本（血清、细胞、试剂等）
的冻结/解冻历程：

- 客户端按样本提交**带事件时间**的温度读数，允许**迟到/乱序**提交，服务端按
  `event_time` 重排后重放；
- 使用**可配置的冻结/解冻阈值 + 滞回（hysteresis）规则**识别状态翻转与完整
  冻融循环；
- 重复事件**幂等去重**（同一样本同一事件时间，或客户端幂等键）；
- 提供样本**当前状态、循环次数、循环明细、状态翻转**与**超限告警**查询接口；
- **启动即初始化数据库**（自动建目录、建表），容器数据持久化到 Docker volume。

---

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与启动初始化（lifespan）
│   ├── config.py      # 环境变量配置（阈值/温度范围/循环上限）
│   ├── db.py          # SQLite 连接、建表 schema、WAL
│   ├── schemas.py     # Pydantic 请求/响应模型
│   └── services.py    # 幂等写入、滞回状态机、循环派生、告警重算
├── tests/test_api.py  # pytest 端到端测试
├── examples/          # 示例请求（curl 脚本 + JSON 载荷）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

服务在 <http://localhost:8000> 启动，数据库文件位于容器内 `/data`，通过名为
`freeze-thaw-data` 的 volume 持久化。首次启动即自动建库建表，无需任何迁移命令。

- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>

停止：`docker compose down`（数据保留在 volume 中；加 `-v` 可一并删除数据）。

## 本地运行（不用 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 数据库默认 /data/freeze_thaw.db，本地可用环境变量覆盖：
DATABASE_PATH=./data/freeze_thaw.db uvicorn app.main:app
```

运行测试：

```bash
pip install -r requirements-dev.txt
pytest -q
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `/data/freeze_thaw.db` | SQLite 数据库文件路径 |
| `FREEZE_THRESHOLD` | `-20` | 全局默认冻结阈值（°C），温度 `<=` 此值进入 frozen |
| `THAW_THRESHOLD` | `0` | 全局默认解冻阈值（°C），温度 `>=` 此值进入 thawed |
| `INITIAL_STATE` | `thawed` | 新样本的初始状态：`frozen` / `thawed` |
| `MIN_ALLOWED_TEMP` | 空（不限制） | 允许的最低温度，低于产生 `temp_excursion_low` 告警（warning） |
| `MAX_ALLOWED_TEMP` | 空（不限制） | 允许的最高温度，高于产生 `temp_excursion_high` 告警（critical） |
| `MAX_FREEZE_THAW_CYCLES` | 空（不限制） | 每个样本允许的完整循环上限，超过产生 `cycles_exceeded` 告警（critical） |

全局默认值可在 **登记样本时按样本覆盖**（阈值、温度范围、循环上限、初始状态）。
约束：`freeze_threshold` 必须严格小于 `thaw_threshold`。

## 滞回规则与循环定义

阈值之间的开区间 `(freeze_threshold, thaw_threshold)` 为**滞回带**，带内温度
**不引起状态变化**，避免在阈值附近抖动造成虚假翻转：

```
温度 <= -20°C          → frozen
温度 >=   0°C          → thawed
-20 < 温度 < 0（带内） → 保持当前状态
```

- **完整冻融循环**：`thawed → frozen`（循环开始）再 `frozen → thawed`（循环结束）。
  在解冻瞬间计数 +1；循环期间的最低/最高温度与持续时长一并给出。
- 样本当前处于冻结中、尚未解冻时，`/status` 中会返回一个 `complete=false` 的
  **未闭合循环**（`/cycles` 也会包含它）。
- 初始状态假设样本在第一次读数之前处于 `initial_state`；因此若样本从 `thawed`
  开始，第一次读数即 -25°C 会开启第一个循环。

## 迟到数据如何处理

服务端**不依赖写入顺序**：每次提交后，都取出该样本**全部读数按
`event_time` 升序排序后重新重放**，据此重建状态翻转记录、当前状态和告警。
因此：

- 先收到较晚的读数、后补录较早的读数，最终结果与数据按时到达完全一致；
- 状态与告警每次全量重建，历史补录会自动修正此前结论，不会重复累积。

## 幂等去重

读数接口对同一批次/多次重试安全：

1. **同一样本 + 同一 `event_time`** 的读数已存在 → 标记 `duplicate` 拒绝
   （先到先得，即使新请求温度不同也不覆盖）；
2. 请求携带 **`idempotency_key`** 时，同一样本下同一幂等键只能成功写入一次，
   重复提交（即使事件时间不同）→ 标记 `duplicate`；
3. 单条重复不会导致整批失败，响应中逐条给出 `inserted` / `duplicate` 及原因。

`event_time` 必须是**带时区的 ISO-8601**（如 `2026-09-20T08:00:00Z`），
服务端统一转成 UTC 存储与排序。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/samples` | 登记样本（可覆盖阈值） |
| `GET` | `/samples` | 样本列表 |
| `GET` | `/samples/{id}` | 样本配置 |
| `POST` | `/samples/{id}/readings` | 批量提交温度读数（去重 + 重算） |
| `GET` | `/samples/{id}/readings` | 该样本全部读数（按事件时间） |
| `GET` | `/samples/{id}/status` | **当前状态、完整循环次数**、未闭合循环 |
| `GET` | `/samples/{id}/cycles` | 完整循环明细（含未闭合循环） |
| `GET` | `/samples/{id}/transitions` | 状态翻转记录 |
| `GET` | `/alerts` | **告警查询**，支持 `sample_id` / `alert_type` / `severity` / `limit` 过滤 |

## 示例请求

```bash
# 1. 登记样本
curl -X POST http://localhost:8000/samples \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "SAMPLE-001",
    "name": "血清样本 A",
    "freeze_threshold": -20.0,
    "thaw_threshold": 0.0,
    "initial_state": "thawed",
    "min_allowed_temp": -80.0,
    "max_allowed_temp": 10.0,
    "max_freeze_thaw_cycles": 3
  }'

# 2. 提交读数（乱序也没关系；可用 idempotency_key 做客户端去重）
curl -X POST http://localhost:8000/samples/SAMPLE-001/readings \
  -H 'Content-Type: application/json' \
  -d '{"readings": [
    {"event_time": "2026-09-20T12:00:00Z", "temperature_c": 3.0},
    {"event_time": "2026-09-20T09:05:00Z", "temperature_c": -22.5},
    {"event_time": "2026-09-20T09:00:00Z", "temperature_c": 4.0,
     "idempotency_key": "device-42-0001"}
  ]}'
# => {"received":3,"inserted":3,"duplicate":0,"results":[...]}

# 3. 当前状态与循环次数
curl http://localhost:8000/samples/SAMPLE-001/status
# => {"current_state":"thawed","completed_cycles":1,...}

# 4. 循环明细（起止时间、时长、区间最低/最高温度）
curl http://localhost:8000/samples/SAMPLE-001/cycles

# 5. 告警查询（可按样本/类型/级别过滤）
curl 'http://localhost:8000/alerts?severity=critical'
curl 'http://localhost:8000/alerts?sample_id=SAMPLE-001&alert_type=temp_excursion_low'
```

完整可运行演示：

```bash
./examples/example_requests.sh
```

## 设计说明

- **并发**：写入接口使用 SQLite `BEGIN IMMEDIATE` 立即事务串行化，配合
  `busy_timeout`；每个请求独立连接，WAL 模式下读写互不阻塞。
- **告警重建**：由于迟到数据可能让旧结论失效，`transitions` 与 `alerts` 在每次
  写入后对该样本全量重建；读数原始表只追加（去重），是唯一事实来源。
- **温度超限**与状态翻转解耦：即使样本一直在冻结区间，越界读数也会告警；
  每个越界读数产生一条告警。
- 循环上限按"完成次数 `>` 上限"判定，仅在解冻完成瞬间触发一次（重放幂等）。
