"""FastAPI 入口与 HTTP 路由。"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, status

from .config import get_settings
from .db import get_conn, init_db
from .schemas import (
    AlertOut,
    CycleOut,
    HealthOut,
    ReadingBatch,
    ReadingBatchResult,
    SampleCreate,
    SampleOut,
    SampleStatusOut,
    TransitionOut,
)
from .services import NotFound, add_readings, create_sample, get_alerts, get_cycles, get_readings, get_sample, get_status, get_transitions, list_samples


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即初始化数据库（建目录、建表、启用 WAL）。
    init_db()
    yield


app = FastAPI(
    title="实验室样本冻融追踪 API",
    description=(
        "按样本提交带事件时间的温度读数；服务端按事件时间重排迟到数据，"
        "基于可配置冻结/解冻阈值与滞回规则识别完整冻融循环，"
        "重复事件幂等去重，并提供当前状态、循环次数与超限告警查询。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

settings = get_settings()


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@app.get("/health", response_model=HealthOut, tags=["meta"], summary="健康检查")
def health() -> HealthOut:
    return HealthOut(status="ok", database=settings.database_path)


# --------------------------------------------------------------------------- #
# 样本
# --------------------------------------------------------------------------- #


@app.post(
    "/samples",
    response_model=SampleOut,
    status_code=status.HTTP_201_CREATED,
    tags=["samples"],
    summary="登记样本（可覆盖阈值）",
)
def post_sample(payload: SampleCreate, conn=Depends(get_conn)) -> SampleOut:
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            out = create_sample(conn, payload, settings)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return out
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail=f"样本 ID 已存在: {payload.id}")
    except ValueError as exc:
        raise _bad_request(exc)


@app.get("/samples", response_model=list[SampleOut], tags=["samples"], summary="列出样本")
def get_samples(conn=Depends(get_conn)) -> list[SampleOut]:
    return list_samples(conn)


@app.get(
    "/samples/{sample_id}",
    response_model=SampleOut,
    tags=["samples"],
    summary="查询样本配置",
)
def get_one_sample(sample_id: str, conn=Depends(get_conn)) -> SampleOut:
    try:
        return get_sample(conn, sample_id)
    except NotFound as exc:
        raise _not_found(exc)


# --------------------------------------------------------------------------- #
# 读数
# --------------------------------------------------------------------------- #


@app.post(
    "/samples/{sample_id}/readings",
    response_model=ReadingBatchResult,
    tags=["readings"],
    summary="提交温度读数（支持迟到数据与幂等去重）",
)
def post_readings(
    sample_id: str, batch: ReadingBatch, conn=Depends(get_conn)
) -> ReadingBatchResult:
    try:
        inserted, results = add_readings(conn, sample_id, batch)
    except NotFound as exc:
        raise _not_found(exc)
    duplicates = len(results) - inserted
    return ReadingBatchResult(
        sample_id=sample_id,
        received=len(results),
        inserted=inserted,
        duplicate=duplicates,
        results=results,
    )


@app.get(
    "/samples/{sample_id}/readings",
    tags=["readings"],
    summary="查询样本的全部读数（按事件时间排序）",
)
def list_readings(sample_id: str, conn=Depends(get_conn)):
    try:
        return get_readings(conn, sample_id)
    except NotFound as exc:
        raise _not_found(exc)


# --------------------------------------------------------------------------- #
# 状态 / 翻转 / 循环
# --------------------------------------------------------------------------- #


@app.get(
    "/samples/{sample_id}/status",
    response_model=SampleStatusOut,
    tags=["tracking"],
    summary="当前状态与循环次数",
)
def sample_status(sample_id: str, conn=Depends(get_conn)) -> SampleStatusOut:
    try:
        return get_status(conn, sample_id)
    except NotFound as exc:
        raise _not_found(exc)


@app.get(
    "/samples/{sample_id}/transitions",
    response_model=list[TransitionOut],
    tags=["tracking"],
    summary="状态翻转记录",
)
def sample_transitions(sample_id: str, conn=Depends(get_conn)) -> list[TransitionOut]:
    try:
        rows = get_transitions(conn, sample_id)
    except NotFound as exc:
        raise _not_found(exc)
    return [TransitionOut(**r) for r in rows]


@app.get(
    "/samples/{sample_id}/cycles",
    response_model=list[CycleOut],
    tags=["tracking"],
    summary="完整冻融循环（及未闭合循环）",
)
def sample_cycles(sample_id: str, conn=Depends(get_conn)) -> list[CycleOut]:
    try:
        return get_cycles(conn, sample_id)
    except NotFound as exc:
        raise _not_found(exc)


# --------------------------------------------------------------------------- #
# 告警
# --------------------------------------------------------------------------- #


@app.get(
    "/alerts",
    response_model=list[AlertOut],
    tags=["alerts"],
    summary="查询超限告警（可按样本/类型/级别过滤）",
)
def list_alerts(
    conn=Depends(get_conn),
    sample_id: str | None = Query(default=None, description="按样本过滤"),
    alert_type: Literal["temp_excursion_low", "temp_excursion_high", "cycles_exceeded"]
    | None = Query(default=None),
    severity: Literal["warning", "critical"] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[AlertOut]:
    try:
        return get_alerts(conn, sample_id, alert_type, severity, limit)
    except NotFound as exc:
        raise _not_found(exc)
