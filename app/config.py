"""应用配置。

阈值既可以通过环境变量做全局默认值，也可以在创建样本时按样本覆盖。
所有温度均为摄氏度 (°C)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    # SQLite 数据库文件路径；容器内默认指向挂载卷。
    database_path: str
    # 进入“冻结”状态需要温度 <= 冻结阈值。
    freeze_threshold: float
    # 进入“解冻”状态需要温度 >= 解冻阈值（必须高于冻结阈值，形成滞回带）。
    thaw_threshold: float
    # 样本登记时假定的初始状态：frozen / thawed。
    initial_state: str
    # 允许的温度范围，越界读数会产生告警；None 表示不检查。
    min_allowed_temp: float | None
    max_allowed_temp: float | None
    # 样本允许经历的完整冻融循环上限，None 表示不限制。
    max_freeze_thaw_cycles: int | None

    @staticmethod
    def from_env() -> "Settings":
        min_raw = os.getenv("MIN_ALLOWED_TEMP", "")
        max_raw = os.getenv("MAX_ALLOWED_TEMP", "")
        cycles_raw = os.getenv("MAX_FREEZE_THAW_CYCLES", "")
        # 环境变量读出来是字符串，必须显式转 float/int，否则后续与读数做比较时
        # 会退化为字符串字典序比较（例如 "-80" vs 11.0）。
        return Settings(
            database_path=os.getenv("DATABASE_PATH", "/data/freeze_thaw.db"),
            freeze_threshold=_get_float("FREEZE_THRESHOLD", -20.0),
            thaw_threshold=_get_float("THAW_THRESHOLD", 0.0),
            initial_state=os.getenv("INITIAL_STATE", "thawed").strip().lower(),
            min_allowed_temp=float(min_raw) if min_raw.strip() else None,
            max_allowed_temp=float(max_raw) if max_raw.strip() else None,
            max_freeze_thaw_cycles=int(cycles_raw) if cycles_raw.strip() else None,
        )


def get_settings() -> Settings:
    return Settings.from_env()
