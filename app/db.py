"""SQLite 连接管理与建表初始化。

应用启动（lifespan）时即初始化数据库：自动创建数据目录、启用 WAL、
建立全部表结构与索引。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator

from .config import Settings, get_settings

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS samples (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    freeze_threshold    REAL NOT NULL,
    thaw_threshold      REAL NOT NULL,
    initial_state       TEXT NOT NULL CHECK (initial_state IN ('frozen', 'thawed')),
    min_allowed_temp    REAL,
    max_allowed_temp    REAL,
    max_cycles          INTEGER,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       TEXT NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    event_time      TEXT NOT NULL,           -- 事件时间（客户端提供），ISO-8601 UTC
    received_at     TEXT NOT NULL,           -- 服务端接收时间
    temperature_c   REAL NOT NULL,
    idempotency_key TEXT,
    -- 同一样本在同一事件时间的读数视为重复；先到先得，后到拒绝。
    UNIQUE (sample_id, event_time),
    -- 客户端可选的幂等键：同一幂等键只能成功写入一次。
    UNIQUE (sample_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_readings_sample_time
    ON readings (sample_id, event_time);

-- 状态机每次发生冻结/解冻状态翻转时记录一行。
CREATE TABLE IF NOT EXISTS transitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       TEXT NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    event_time      TEXT NOT NULL,
    from_state      TEXT NOT NULL CHECK (from_state IN ('frozen', 'thawed')),
    to_state        TEXT NOT NULL CHECK (to_state IN ('frozen', 'thawed')),
    temperature_c   REAL NOT NULL,
    -- 该翻转之后已完成的完整循环数：thawed->frozen 时计数，否则为 NULL。
    cycle_no        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_transitions_sample_time
    ON transitions (sample_id, event_time);

-- 每个样本一行的当前状态汇总。
CREATE TABLE IF NOT EXISTS sample_states (
    sample_id           TEXT PRIMARY KEY REFERENCES samples(id) ON DELETE CASCADE,
    current_state       TEXT NOT NULL CHECK (current_state IN ('frozen', 'thawed')),
    completed_cycles    INTEGER NOT NULL DEFAULT 0,
    last_event_time     TEXT,
    last_temperature_c  REAL,
    updated_at          TEXT NOT NULL
);

-- 告警在每次重算时全量重建（迟到数据可能使旧告警失效）。
CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       TEXT NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    alert_type      TEXT NOT NULL CHECK (
                        alert_type IN ('temp_excursion_low', 'temp_excursion_high',
                                       'cycles_exceeded')
                    ),
    severity        TEXT NOT NULL CHECK (severity IN ('warning', 'critical')),
    event_time      TEXT NOT NULL,
    temperature_c   REAL,
    message         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_sample_time
    ON alerts (sample_id, event_time);
"""


def _connect(database_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        database_path,
        # 同一时刻只允许一个写事务，其他写入请求收到错误而不是静默排队过久。
        timeout=10,
        isolation_level=None,  # autocommit；事务由服务层显式 BEGIN IMMEDIATE 控制
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db(settings: Settings | None = None) -> None:
    """创建数据目录并初始化表结构。进程启动时调用一次。"""
    settings = settings or get_settings()
    db_dir = os.path.dirname(os.path.abspath(settings.database_path))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = _connect(settings.database_path)
    try:
        conn.executescript(_SCHEMA)
    finally:
        conn.close()


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一个连接，结束时关闭。"""
    conn = _connect(get_settings().database_path)
    try:
        yield conn
    finally:
        conn.close()
