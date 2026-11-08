"""Offline tests for the press safety rules. No Bluetooth and no real ping."""
import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("API_TOKEN", TOKEN)
    monkeypatch.setenv("PC_HOST", "100.64.0.2")
    monkeypatch.setenv("FINGERBOT_MAC", "AA:BB:CC:DD:EE:FF")
    monkeypatch.setenv("PRESS_LOG", str(tmp_path / "press.log"))
    monkeypatch.setenv("WATCH_SECONDS", "0")
    import pc_power
    import server
    importlib.reload(pc_power)
    importlib.reload(server)
    presses = []

    async def fake_press(mac, hold=None, down=None):
        presses.append((mac, hold, down))
        return True

    monkeypatch.setattr(pc_power, "_press_once", fake_press)
    monkeypatch.setattr(pc_power, "RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(server, "pc_is_up", lambda host=None: False)
    return pc_power, server, presses, tmp_path / "press.log"


def client(server):
    return TestClient(server.app)


def test_no_token_is_401(env):
    _, server, presses, _ = env
    c = client(server)
    assert c.get("/status").status_code == 401
    assert c.post("/press").status_code == 401
    assert c.post("/press", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert presses == []


def test_refuses_when_pc_answers_ping(env, monkeypatch):
    pc_power, server, presses, log = env
    monkeypatch.setattr(pc_power, "pc_is_up", lambda host=None: True)
    r = client(server).post("/press", headers=AUTH)
    assert r.status_code == 409 and r.json()["pc_up"] is True
    assert presses == []
    assert "reason=pc_reachable" in log.read_text()


def test_press_uses_hold_3_and_depth_100(env, monkeypatch):
    pc_power, server, presses, _ = env
    monkeypatch.setattr(pc_power, "pc_is_up", lambda host=None: False)
    r = client(server).post("/press", headers=AUTH)
    assert r.status_code == 200 and r.json()["pressed"] is True
    assert presses == [("AA:BB:CC:DD:EE:FF", 3, 100)]


def test_cooldown_blocks_second_press(env, monkeypatch):
    pc_power, server, presses, _ = env
    monkeypatch.setattr(pc_power, "pc_is_up", lambda host=None: False)
    c = client(server)
    assert c.post("/press", headers=AUTH).status_code == 200
    r = c.post("/press", headers=AUTH)
    assert r.status_code == 409 and r.json()["seconds_left"] > 0
    assert len(presses) == 1


def test_force_skips_ping_check(env, monkeypatch):
    pc_power, server, presses, _ = env
    monkeypatch.setattr(pc_power, "pc_is_up", lambda host=None: True)
    r = client(server).post("/press?force=true", headers=AUTH)
    assert r.status_code == 200 and len(presses) == 1


def test_no_retry_after_press_was_sent(env, monkeypatch):
    pc_power, server, _, log = env
    monkeypatch.setattr(pc_power, "pc_is_up", lambda host=None: False)
    calls = []

    async def fails_after_write(mac, hold=None, down=None):
        calls.append(1)
        raise pc_power.PressSentError("disconnected after write")

    monkeypatch.setattr(pc_power, "_press_once", fails_after_write)
    r = client(server).post("/press", headers=AUTH)
    assert r.status_code == 502 and len(calls) == 1


def test_ble_errors_before_write_retry_3_times(env, monkeypatch):
    pc_power, server, _, _ = env
    from bleak import BleakError
    monkeypatch.setattr(pc_power, "pc_is_up", lambda host=None: False)
    calls = []

    async def not_found(mac, hold=None, down=None):
        calls.append(1)
        raise BleakError("Fingerbot was not found")

    monkeypatch.setattr(pc_power, "_press_once", not_found)
    r = client(server).post("/press", headers=AUTH)
    assert r.status_code == 502 and len(calls) == 3


def test_restart_needs_confirm_then_holds_10_then_3(env, monkeypatch):
    pc_power, server, presses, _ = env
    monkeypatch.setattr(pc_power, "pc_is_up", lambda host=None: True)
    monkeypatch.setattr(pc_power, "RESTART_GAP_SECONDS", 0)
    c = client(server)
    assert c.post("/restart", headers=AUTH).status_code == 400
    assert presses == []
    r = c.post("/restart", headers=AUTH, json={"confirm": True})
    assert r.status_code == 200
    assert [h for _, h, _ in presses] == [10, 3]


def test_missing_mac_is_500(env, monkeypatch):
    pc_power, server, presses, _ = env
    monkeypatch.delenv("FINGERBOT_MAC")
    monkeypatch.delenv("SWITCHBOT_MAC", raising=False)
    monkeypatch.setattr(pc_power, "pc_is_up", lambda host=None: False)
    r = client(server).post("/press", headers=AUTH)
    assert r.status_code == 500 and presses == []
