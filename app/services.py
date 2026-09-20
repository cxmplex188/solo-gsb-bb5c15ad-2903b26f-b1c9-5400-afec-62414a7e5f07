"""核心业务逻辑：幂等写入、按事件时间重放的滞回状态机与告警重算。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .config import Settings
from .schemas import (
    AlertOut,
    CycleOut,
    ReadingBatch,
    ReadingItemResult,
    SampleCreate,
    SampleOut,
    SampleStatusOut,
    to_utc_iso,
)


class NotFound(LookupError):
    """样本不存在。"""


def _now_iso() -> str:
    return to_utc_iso(datetime.now(timezone.utc))


def _sample_to_out(row: sqlite3.Row) -> SampleOut:
    return SampleOut(
        id=row["id"],
        name=row["name"],
        freeze_threshold=row["freeze_threshold"],
        thaw_threshold=row["thaw_threshold"],
        initial_state=row["initial_state"],
        min_allowed_temp=row["min_allowed_temp"],
        max_allowed_temp=row["max_allowed_temp"],
        max_freeze_thaw_cycles=row["max_cycles"],
        created_at=row["created_at"],
    )


# --------------------------------------------------------------------------- #
# 样本管理
# --------------------------------------------------------------------------- #


def create_sample(conn: sqlite3.Connection, payload: SampleCreate, settings: Settings) -> SampleOut:
    """登记样本；未提供的阈值由全局配置补齐。"""
    freeze = (
        payload.freeze_threshold
        if payload.freeze_threshold is not None
        else settings.freeze_threshold
    )
    thaw = (
        payload.thaw_threshold
        if payload.thaw_threshold is not None
        else settings.thaw_threshold
    )
    initial = payload.initial_state or settings.initial_state
    if initial not in ("frozen", "thawed"):
        raise ValueError("initial_state 只能是 frozen 或 thawed")
    if freeze >= thaw:
        raise ValueError("freeze_threshold 必须严格小于 thaw_threshold")

    # 温度允许范围：两个边界只要显式给了任意一个，就以请求为准（另一个为不限制）；
    # 两个都不给时才整体使用全局默认配置。
    if payload.min_allowed_temp is not None or payload.max_allowed_temp is not None:
        min_allowed = payload.min_allowed_temp
        max_allowed = payload.max_allowed_temp
    else:
        min_allowed = settings.min_allowed_temp
        max_allowed = settings.max_allowed_temp

    # 循环上限未提供时使用全局默认。
    max_cycles = (
        payload.max_freeze_thaw_cycles
        if payload.max_freeze_thaw_cycles is not None
        else settings.max_freeze_thaw_cycles
    )

    conn.execute(
        """
        INSERT INTO samples (id, name, freeze_threshold, thaw_threshold,
                             initial_state, min_allowed_temp, max_allowed_temp,
                             max_cycles, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.id,
            payload.name,
            freeze,
            thaw,
            initial,
            min_allowed,
            max_allowed,
            max_cycles,
            _now_iso(),
        ),
    )
    # 新样本初始即 0 次完整循环。
    conn.execute(
        """
        INSERT INTO sample_states (sample_id, current_state, completed_cycles,
                                   last_event_time, last_temperature_c, updated_at)
        VALUES (?, ?, 0, NULL, NULL, ?)
        """,
        (payload.id, initial, _now_iso()),
    )
    row = conn.execute("SELECT * FROM samples WHERE id = ?", (payload.id,)).fetchone()
    return _sample_to_out(row)


def list_samples(conn: sqlite3.Connection) -> list[SampleOut]:
    rows = conn.execute("SELECT * FROM samples ORDER BY id").fetchall()
    return [_sample_to_out(r) for r in rows]


def get_sample(conn: sqlite3.Connection, sample_id: str) -> SampleOut:
    row = conn.execute("SELECT * FROM samples WHERE id = ?", (sample_id,)).fetchone()
    if row is None:
        raise NotFound(f"样本不存在: {sample_id}")
    return _sample_to_out(row)


# --------------------------------------------------------------------------- #
# 读数写入（幂等去重）+ 迟到数据重排重算
# --------------------------------------------------------------------------- #


def add_readings(
    conn: sqlite3.Connection,
    sample_id: str,
    batch: ReadingBatch,
) -> tuple[int, list[ReadingItemResult]]:
    """在一个立即写事务中完成：去重写入 + 全量按事件时间重放。

    迟到数据无需特殊处理——状态与告警完全由按 event_time 排序后的重放推导，
    因此历史读数补录后结果自动修正。
    """
    sample = conn.execute(
        "SELECT * FROM samples WHERE id = ?", (sample_id,)
    ).fetchone()
    if sample is None:
        raise NotFound(f"样本不存在: {sample_id}")

    conn.execute("BEGIN IMMEDIATE")
    try:
        results: list[ReadingItemResult] = []
        inserted = 0
        for item in batch.readings:
            iso = to_utc_iso(item.event_time)
            key = item.idempotency_key
            try:
                conn.execute(
                    """
                    INSERT INTO readings (sample_id, event_time, received_at,
                                          temperature_c, idempotency_key)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (sample_id, iso, _now_iso(), item.temperature_c, key),
                )
                inserted += 1
                results.append(
                    ReadingItemResult(
                        event_time=iso,
                        temperature_c=item.temperature_c,
                        idempotency_key=key,
                        status="inserted",
                    )
                )
            except sqlite3.IntegrityError:
                # 命中 (sample_id, event_time) 或 (sample_id, idempotency_key) 唯一约束。
                detail = _duplicate_detail(conn, sample_id, iso, key)
                results.append(
                    ReadingItemResult(
                        event_time=iso,
                        temperature_c=item.temperature_c,
                        idempotency_key=key,
                        status="duplicate",
                        detail=detail,
                    )
                )

        recompute_sample(conn, sample)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return inserted, results


def _duplicate_detail(
    conn: sqlite3.Connection,
    sample_id: str,
    event_time: str,
    key: str | None,
) -> str:
    row = conn.execute(
        "SELECT temperature_c, idempotency_key FROM readings "
        "WHERE sample_id = ? AND event_time = ?",
        (sample_id, event_time),
    ).fetchone()
    if row is not None:
        return (
            f"该事件时间的读数已存在（先到先得，温度 {row['temperature_c']}°C，"
            f"幂等键 {row['idempotency_key']!r}），已忽略重复提交"
        )
    if key is not None:
        row = conn.execute(
            "SELECT event_time FROM readings WHERE sample_id = ? AND idempotency_key = ?",
            (sample_id, key),
        ).fetchone()
        if row is not None:
            return f"幂等键 {key!r} 已用于事件时间 {row['event_time']} 的读数"
    return "重复读数"


# --------------------------------------------------------------------------- #
# 状态机重放（滞回规则）
# --------------------------------------------------------------------------- #


def recompute_sample(conn: sqlite3.Connection, sample: sqlite3.Row) -> None:
    """按事件时间顺序重放全部读数，重建 transitions / alerts / 当前状态。

    滞回规则：
      * 温度 <= freeze_threshold 才进入 frozen；
      * 温度 >= thaw_threshold 才进入 thawed；
      * 滞回区间 (freeze, thaw) 内温度不引起状态翻转。
    完整冻融循环：thawed -> frozen 开始，frozen -> thawed 完成并计数。
    """
    sid = sample["id"]
    freeze_t = sample["freeze_threshold"]
    thaw_t = sample["thaw_threshold"]
    min_allowed = sample["min_allowed_temp"]
    max_allowed = sample["max_allowed_temp"]
    max_cycles = sample["max_cycles"]

    conn.execute("DELETE FROM transitions WHERE sample_id = ?", (sid,))
    conn.execute("DELETE FROM alerts WHERE sample_id = ?", (sid,))

    readings = conn.execute(
        "SELECT event_time, temperature_c FROM readings "
        "WHERE sample_id = ? ORDER BY event_time, id",
        (sid,),
    ).fetchall()

    state = sample["initial_state"]
    completed_cycles = 0
    last_event: str | None = None
    last_temp: float | None = None

    for r in readings:
        iso, temp = r["event_time"], r["temperature_c"]
        new_state = state
        if temp <= freeze_t:
            new_state = "frozen"
        elif temp >= thaw_t:
            new_state = "thawed"
        # freeze_t < temp < thaw_t：滞回带内，保持原状态。

        if new_state != state:
            cycle_no = completed_cycles + 1 if new_state == "frozen" else None
            conn.execute(
                """
                INSERT INTO transitions (sample_id, event_time, from_state,
                                         to_state, temperature_c, cycle_no)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sid, iso, state, new_state, temp, cycle_no),
            )
            state = new_state
            if new_state == "thawed":
                completed_cycles += 1
                if max_cycles is not None and completed_cycles > max_cycles:
                    conn.execute(
                        """
                        INSERT INTO alerts (sample_id, alert_type, severity,
                                           event_time, temperature_c, message)
                        VALUES (?, 'cycles_exceeded', 'critical', ?, ?, ?)
                        """,
                        (
                            sid,
                            iso,
                            temp,
                            f"已完成 {completed_cycles} 次冻融循环，"
                            f"超过上限 {max_cycles} 次",
                        ),
                    )

        # 温度超限告警（与状态翻转无关，每个越界读数一条）。
        if min_allowed is not None and temp < min_allowed:
            conn.execute(
                """
                INSERT INTO alerts (sample_id, alert_type, severity,
                                   event_time, temperature_c, message)
                VALUES (?, 'temp_excursion_low', 'warning', ?, ?, ?)
                """,
                (sid, iso, temp, f"温度 {temp}°C 低于允许下限 {min_allowed}°C"),
            )
        if max_allowed is not None and temp > max_allowed:
            conn.execute(
                """
                INSERT INTO alerts (sample_id, alert_type, severity,
                                   event_time, temperature_c, message)
                VALUES (?, 'temp_excursion_high', 'critical', ?, ?, ?)
                """,
                (sid, iso, temp, f"温度 {temp}°C 高于允许上限 {max_allowed}°C"),
            )

        last_event, last_temp = iso, temp

    conn.execute(
        """
        INSERT INTO sample_states (sample_id, current_state, completed_cycles,
                                   last_event_time, last_temperature_c, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(sample_id) DO UPDATE SET
            current_state=excluded.current_state,
            completed_cycles=excluded.completed_cycles,
            last_event_time=excluded.last_event_time,
            last_temperature_c=excluded.last_temperature_c,
            updated_at=excluded.updated_at
        """,
        (sid, state, completed_cycles, last_event, last_temp, _now_iso()),
    )


# --------------------------------------------------------------------------- #
# 循环派生与状态查询
# --------------------------------------------------------------------------- #


def _derive_cycles(
    readings: list[sqlite3.Row], transitions: list[sqlite3.Row]
) -> tuple[list[CycleOut], CycleOut | None]:
    """从读数与翻转记录派生完整循环，以及当前可能未闭合的循环。"""
    trans_at = {t["event_time"]: t for t in transitions}
    completed: list[CycleOut] = []
    current: dict | None = None

    for r in readings:
        iso, temp = r["event_time"], r["temperature_c"]
        t = trans_at.get(iso)
        if t is not None and t["from_state"] == "thawed" and t["to_state"] == "frozen":
            # 新循环开始（理论上上一个循环已闭合）。
            current = {
                "cycle_no": t["cycle_no"],
                "started_at": iso,
                "min": temp,
                "max": temp,
            }
        elif current is not None:
            current["min"] = min(current["min"], temp)
            current["max"] = max(current["max"], temp)
            if t is not None and t["to_state"] == "thawed":
                start = datetime.fromisoformat(current["started_at"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                completed.append(
                    CycleOut(
                        cycle_no=current["cycle_no"],
                        started_at=current["started_at"],
                        ended_at=iso,
                        duration_seconds=(end - start).total_seconds(),
                        min_temperature_c=current["min"],
                        max_temperature_c=current["max"],
                        complete=True,
                    )
                )
                current = None

    open_cycle: CycleOut | None = None
    if current is not None:
        start = datetime.fromisoformat(current["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(readings[-1]["event_time"].replace("Z", "+00:00"))
        open_cycle = CycleOut(
            cycle_no=current["cycle_no"],
            started_at=current["started_at"],
            ended_at=readings[-1]["event_time"],
            duration_seconds=(end - start).total_seconds(),
            min_temperature_c=current["min"],
            max_temperature_c=current["max"],
            complete=False,
        )
    return completed, open_cycle


def get_status(conn: sqlite3.Connection, sample_id: str) -> SampleStatusOut:
    sample = conn.execute(
        "SELECT * FROM samples WHERE id = ?", (sample_id,)
    ).fetchone()
    if sample is None:
        raise NotFound(f"样本不存在: {sample_id}")
    state_row = conn.execute(
        "SELECT * FROM sample_states WHERE sample_id = ?", (sample_id,)
    ).fetchone()
    readings = conn.execute(
        "SELECT event_time, temperature_c FROM readings "
        "WHERE sample_id = ? ORDER BY event_time, id",
        (sample_id,),
    ).fetchall()
    transitions = conn.execute(
        "SELECT event_time, from_state, to_state, temperature_c, cycle_no "
        "FROM transitions WHERE sample_id = ? ORDER BY event_time, id",
        (sample_id,),
    ).fetchall()
    _, open_cycle = _derive_cycles(readings, transitions)

    return SampleStatusOut(
        sample_id=sample["id"],
        name=sample["name"],
        current_state=state_row["current_state"],
        completed_cycles=state_row["completed_cycles"],
        last_event_time=state_row["last_event_time"],
        last_temperature_c=state_row["last_temperature_c"],
        open_cycle=open_cycle,
    )


def get_transitions(conn: sqlite3.Connection, sample_id: str) -> list[dict]:
    get_sample(conn, sample_id)  # 存在性检查
    rows = conn.execute(
        """
        SELECT event_time, from_state, to_state, temperature_c, cycle_no
        FROM transitions WHERE sample_id = ? ORDER BY event_time, id
        """,
        (sample_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_cycles(conn: sqlite3.Connection, sample_id: str) -> list[CycleOut]:
    get_sample(conn, sample_id)
    readings = conn.execute(
        "SELECT event_time, temperature_c FROM readings "
        "WHERE sample_id = ? ORDER BY event_time, id",
        (sample_id,),
    ).fetchall()
    transitions = conn.execute(
        "SELECT event_time, from_state, to_state, temperature_c, cycle_no "
        "FROM transitions WHERE sample_id = ? ORDER BY event_time, id",
        (sample_id,),
    ).fetchall()
    completed, open_cycle = _derive_cycles(readings, transitions)
    return completed + ([open_cycle] if open_cycle else [])


def get_readings(conn: sqlite3.Connection, sample_id: str) -> list[dict]:
    get_sample(conn, sample_id)
    rows = conn.execute(
        """
        SELECT event_time, received_at, temperature_c, idempotency_key
        FROM readings WHERE sample_id = ? ORDER BY event_time, id
        """,
        (sample_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_alerts(
    conn: sqlite3.Connection,
    sample_id: str | None,
    alert_type: str | None,
    severity: str | None,
    limit: int,
) -> list[AlertOut]:
    sql = "SELECT * FROM alerts WHERE 1=1"
    params: list = []
    if sample_id is not None:
        # 若指定了不存在的样本，按 404 处理更直观。
        get_sample(conn, sample_id)
        sql += " AND sample_id = ?"
        params.append(sample_id)
    if alert_type is not None:
        sql += " AND alert_type = ?"
        params.append(alert_type)
    if severity is not None:
        sql += " AND severity = ?"
        params.append(severity)
    sql += " ORDER BY event_time, id LIMIT ?"
    params.append(limit)
    return [AlertOut(**dict(r)) for r in conn.execute(sql, params).fetchall()]
