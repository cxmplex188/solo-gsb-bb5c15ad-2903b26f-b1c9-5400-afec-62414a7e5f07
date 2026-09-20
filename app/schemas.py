"""Pydantic 请求/响应模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

State = Literal["frozen", "thawed"]


def to_utc_iso(dt: datetime) -> str:
    """统一输出为带 Z 的 UTC ISO-8601 字符串。"""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class SampleCreate(BaseModel):
    id: str = Field(..., min_length=1, max_length=128, examples=["SAMPLE-001"])
    name: str = Field(..., min_length=1, max_length=256, examples=["血清样本 A"])
    # 全部可选；缺省时由服务层填入环境变量中的全局默认值。
    freeze_threshold: float | None = Field(default=None, examples=[-20.0])
    thaw_threshold: float | None = Field(default=None, examples=[0.0])
    initial_state: State | None = None
    min_allowed_temp: float | None = None
    max_allowed_temp: float | None = None
    max_freeze_thaw_cycles: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_thresholds(self) -> "SampleCreate":
        freeze = self.freeze_threshold
        thaw = self.thaw_threshold
        if freeze is not None and thaw is not None and freeze >= thaw:
            raise ValueError(
                "freeze_threshold 必须严格小于 thaw_threshold（滞回区间不能为空）"
            )
        if (
            self.min_allowed_temp is not None
            and self.max_allowed_temp is not None
            and self.min_allowed_temp > self.max_allowed_temp
        ):
            raise ValueError("min_allowed_temp 不能大于 max_allowed_temp")
        return self


class ReadingIn(BaseModel):
    """单个温度读数。event_time 是读数实际发生时间，允许晚到。"""

    event_time: datetime = Field(..., examples=["2026-09-20T08:00:00Z"])
    temperature_c: float = Field(..., examples=[-22.5])
    # 可选客户端幂等键（如消息 ID / 设备流水号）。
    idempotency_key: str | None = Field(default=None, max_length=128)

    @field_validator("event_time")
    @classmethod
    def _require_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("event_time 必须带时区信息，例如 2026-09-20T08:00:00Z")
        return v.astimezone(timezone.utc)


class ReadingBatch(BaseModel):
    readings: list[ReadingIn] = Field(..., min_length=1, max_length=1000)


class ReadingItemResult(BaseModel):
    event_time: str
    temperature_c: float
    idempotency_key: str | None
    status: Literal["inserted", "duplicate"]
    detail: str | None = None


class ReadingBatchResult(BaseModel):
    sample_id: str
    received: int
    inserted: int
    duplicate: int
    results: list[ReadingItemResult]


class SampleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    freeze_threshold: float
    thaw_threshold: float
    initial_state: State
    min_allowed_temp: float | None
    max_allowed_temp: float | None
    max_freeze_thaw_cycles: int | None
    created_at: str


class TransitionOut(BaseModel):
    event_time: str
    from_state: State
    to_state: State
    temperature_c: float
    cycle_no: int | None


class CycleOut(BaseModel):
    """完整循环 = thawed -> frozen 开始，frozen -> thawed 结束。"""

    cycle_no: int
    started_at: str
    ended_at: str
    duration_seconds: float
    min_temperature_c: float
    max_temperature_c: float
    complete: bool = True


class SampleStatusOut(BaseModel):
    sample_id: str
    name: str
    current_state: State
    completed_cycles: int
    last_event_time: str | None
    last_temperature_c: float | None
    open_cycle: CycleOut | None


class AlertOut(BaseModel):
    id: int
    sample_id: str
    alert_type: Literal["temp_excursion_low", "temp_excursion_high", "cycles_exceeded"]
    severity: Literal["warning", "critical"]
    event_time: str
    temperature_c: float | None
    message: str


class HealthOut(BaseModel):
    status: Literal["ok"]
    database: str
