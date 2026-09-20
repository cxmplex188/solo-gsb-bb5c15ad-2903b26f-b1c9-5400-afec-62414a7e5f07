"""冻融追踪 API 端到端测试（含滞回、迟到数据、幂等与告警）。"""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    """每个测试使用独立数据库，重放模块初始化以读取新的 DATABASE_PATH。"""
    tmp = tempfile.mkdtemp(prefix="ft-test-")
    monkeypatch.setenv("DATABASE_PATH", os.path.join(tmp, "t.db"))

    import importlib

    from app import config, db, main, schemas, services  # noqa: F401

    for mod in (config, db, services, main):
        importlib.reload(mod)

    with TestClient(main.app) as c:
        yield c


def create_sample(client, sid="S1", **overrides):
    body = {
        "id": sid,
        "name": "测试样本",
        "freeze_threshold": -20.0,
        "thaw_threshold": 0.0,
        "initial_state": "thawed",
        "min_allowed_temp": -80.0,
        "max_allowed_temp": 10.0,
    }
    body.update(overrides)
    r = client.post("/samples", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def post(client, sid, readings):
    return client.post(f"/samples/{sid}/readings", json={"readings": readings})


def test_health_and_sample_lifecycle(client):
    h = client.get("/health")
    assert h.status_code == 200
    assert h.json()["status"] == "ok"

    create_sample(client)
    assert client.get("/samples/S1").status_code == 200
    assert any(s["id"] == "S1" for s in client.get("/samples").json())

    # 重复 ID -> 409
    r = client.post("/samples", json={"id": "S1", "name": "重复"})
    assert r.status_code == 409


def test_hysteresis_and_complete_cycle(client):
    create_sample(client)
    # -10°C 位于滞回带 (-20, 0) 内：初始 thawed，不应翻转
    r = post(client, "S1", [{"event_time": "2026-09-20T08:00:00Z", "temperature_c": -10.0}])
    assert r.json()["inserted"] == 1
    assert client.get("/samples/S1/status").json()["current_state"] == "thawed"

    # <= -20°C 才冻结；阈值本身（边界）触发
    post(client, "S1", [{"event_time": "2026-09-20T09:00:00Z", "temperature_c": -20.0}])
    st = client.get("/samples/S1/status").json()
    assert st["current_state"] == "frozen"
    assert st["completed_cycles"] == 0
    assert st["open_cycle"] is not None
    assert st["open_cycle"]["complete"] is False

    # 滞回带内温度不触发解冻
    post(client, "S1", [{"event_time": "2026-09-20T10:00:00Z", "temperature_c": -1.0}])
    assert client.get("/samples/S1/status").json()["current_state"] == "frozen"

    # >= 0°C 解冻，完成第 1 个完整循环
    post(client, "S1", [{"event_time": "2026-09-20T11:00:00Z", "temperature_c": 0.5}])
    st = client.get("/samples/S1/status").json()
    assert st["current_state"] == "thawed"
    assert st["completed_cycles"] == 1
    assert st["open_cycle"] is None

    cycles = client.get("/samples/S1/cycles").json()
    assert len(cycles) == 1
    c0 = cycles[0]
    assert c0["complete"] is True
    assert c0["started_at"] == "2026-09-20T09:00:00Z"
    assert c0["ended_at"] == "2026-09-20T11:00:00Z"
    assert c0["duration_seconds"] == 2 * 3600
    assert c0["min_temperature_c"] == -20.0
    assert c0["max_temperature_c"] == 0.5

    transitions = client.get("/samples/S1/transitions").json()
    assert [(t["from_state"], t["to_state"]) for t in transitions] == [
        ("thawed", "frozen"),
        ("frozen", "thawed"),
    ]


def test_late_data_is_reordered(client):
    create_sample(client)
    # 先只提交“解冻时刻”，此时从未冻结，不应产生任何翻转
    post(client, "S1", [{"event_time": "2026-09-20T11:00:00Z", "temperature_c": 2.0}])
    st = client.get("/samples/S1/status").json()
    assert st["current_state"] == "thawed"
    assert st["completed_cycles"] == 0

    # 迟到补录更早的冻结数据；重放后应得到完整循环
    post(client, "S1", [
        {"event_time": "2026-09-20T09:00:00Z", "temperature_c": -25.0},
        {"event_time": "2026-09-20T10:00:00Z", "temperature_c": -24.0},
    ])
    st = client.get("/samples/S1/status").json()
    assert st["current_state"] == "thawed"
    assert st["completed_cycles"] == 1
    cycles = client.get("/samples/S1/cycles").json()
    assert len(cycles) == 1
    assert cycles[0]["min_temperature_c"] == -25.0


def test_idempotent_dedup(client):
    create_sample(client)
    payload = [
        {"event_time": "2026-09-20T09:00:00Z", "temperature_c": -22.0},
        # 同一事件时间再次提交（即便温度不同）→ duplicate
        {"event_time": "2026-09-20T09:00:00Z", "temperature_c": -99.0},
    ]
    r = post(client, "S1", payload).json()
    assert r["inserted"] == 1
    assert r["duplicate"] == 1
    assert r["results"][1]["status"] == "duplicate"

    # 整批重试：全部 duplicate，循环次数不重复累计
    r = post(client, "S1", payload[:1]).json()
    assert r["inserted"] == 0
    assert r["results"][0]["status"] == "duplicate"
    assert client.get("/samples/S1/status").json()["completed_cycles"] == 0

    # 幂等键：同键第二次（不同事件时间）→ duplicate；用滞回带内温度避免状态翻转
    r = post(client, "S1", [
        {"event_time": "2026-09-20T12:00:00Z", "temperature_c": -5.0,
         "idempotency_key": "dev-42-1"},
    ]).json()
    assert r["inserted"] == 1
    r = post(client, "S1", [
        {"event_time": "2026-09-20T13:00:00Z", "temperature_c": -6.0,
         "idempotency_key": "dev-42-1"},
    ]).json()
    assert r["duplicate"] == 1

    # 首次读数（-22）仍生效，样本处于 frozen，循环未闭合
    st = client.get("/samples/S1/status").json()
    assert st["current_state"] == "frozen"
    assert st["completed_cycles"] == 0
    assert st["open_cycle"] is not None


def test_alerts_temperature_and_cycles(client):
    create_sample(client, max_freeze_thaw_cycles=1)
    post(client, "S1", [
        {"event_time": "2026-09-20T08:00:00Z", "temperature_c": -85.0},   # 冻结且低于下限
        {"event_time": "2026-09-20T09:00:00Z", "temperature_c": 11.0},    # 解冻（第1循环=上限）且高于上限
        {"event_time": "2026-09-20T10:00:00Z", "temperature_c": -22.0},   # 再冻结
        {"event_time": "2026-09-20T11:00:00Z", "temperature_c": 2.0},     # 再解冻（第2循环 -> 超限）
    ])

    alerts = client.get("/alerts?sample_id=S1").json()
    types = sorted(a["alert_type"] for a in alerts)
    assert types.count("temp_excursion_low") == 1
    assert types.count("temp_excursion_high") == 1
    assert types.count("cycles_exceeded") == 1

    low = next(a for a in alerts if a["alert_type"] == "temp_excursion_low")
    assert low["severity"] == "warning"
    cyc = next(a for a in alerts if a["alert_type"] == "cycles_exceeded")
    assert cyc["severity"] == "critical"

    # 过滤器
    crit = client.get("/alerts?sample_id=S1&severity=critical").json()
    assert {a["alert_type"] for a in crit} == {"temp_excursion_high", "cycles_exceeded"}

    # 全局告警查询（不带样本过滤）同样包含这些告警
    assert len(client.get("/alerts").json()) >= 3


def test_late_data_retracts_cycle_alert_semantics(client):
    """补录中间读数不会虚增循环；告警数量与重放结果严格一致。"""
    create_sample(client, max_freeze_thaw_cycles=1)
    post(client, "S1", [
        {"event_time": "2026-09-20T10:00:00Z", "temperature_c": -22.0},
        {"event_time": "2026-09-20T11:00:00Z", "temperature_c": 2.0},     # 第1循环
        {"event_time": "2026-09-20T12:00:00Z", "temperature_c": -22.0},
        {"event_time": "2026-09-20T13:00:00Z", "temperature_c": 2.0},     # 第2循环 -> 告警
    ])
    assert len(client.get("/alerts?sample_id=S1&alert_type=cycles_exceeded").json()) == 1

    # 再提交任何（去重后的空）写入触发重算，结论保持稳定（不重复累积告警）
    post(client, "S1", [
        {"event_time": "2026-09-20T10:00:00Z", "temperature_c": -22.0},
    ])
    assert len(client.get("/alerts?sample_id=S1&alert_type=cycles_exceeded").json()) == 1


def test_not_found(client):
    assert client.get("/samples/NOPE/status").status_code == 404
    r = post(client, "NOPE", [{"event_time": "2026-09-20T10:00:00Z", "temperature_c": 1.0}])
    assert r.status_code == 404


def test_invalid_thresholds_and_naive_time(client):
    # freeze >= thaw 不允许
    r = client.post("/samples", json={
        "id": "BAD", "name": "x",
        "freeze_threshold": 5.0, "thaw_threshold": 0.0,
    })
    assert r.status_code == 422

    # 不带时区的 event_time 被拒绝
    create_sample(client, "OK")
    r = post(client, "OK", [{"event_time": "2026-09-20T10:00:00", "temperature_c": 1.0}])
    assert r.status_code == 422
