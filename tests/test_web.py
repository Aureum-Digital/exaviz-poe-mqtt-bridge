"""Tests for the embedded web UI API."""
from __future__ import annotations

import pytest
from aiohttp import web as aioweb
from aiohttp.test_utils import TestClient, TestServer

from exaviz_poe_mqtt_bridge.config import BridgeConfig, Config, MqttConfig, WebConfig
from exaviz_poe_mqtt_bridge.daemon import Daemon
from exaviz_poe_mqtt_bridge.web import WebServer


@pytest.fixture
async def client():
    config = Config(
        mqtt=MqttConfig(host="localhost"),
        bridge=BridgeConfig(),
        web=WebConfig(enabled=True),
    )
    daemon = Daemon(config, dry_run=True)
    daemon._port_ids = daemon._simulator.port_ids
    daemon._mqtt.publish = lambda *a, **k: None  # no broker in tests
    daemon.latest_status = await daemon._simulator.read_all()
    daemon.latest_updated = 1234567890.0

    server = WebServer(daemon, config)
    app = aioweb.Application()
    app.router.add_get("/", server._handle_index)
    app.router.add_get("/api/status", server._handle_status)
    app.router.add_post("/api/ports/{port_id}/power", server._handle_power)
    app.router.add_post("/api/ports/{port_id}/reset", server._handle_reset)

    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client, daemon
    await test_client.close()


async def test_index_serves_html(client):
    c, _ = client
    resp = await c.get("/")
    assert resp.status == 200
    assert "text/html" in resp.headers["Content-Type"]
    assert "Exaviz PoE" in await resp.text()


async def test_status_payload(client):
    c, _ = client
    resp = await c.get("/api/status")
    assert resp.status == 200
    data = await resp.json()
    assert data["device"]["name"] == "Exaviz Cruiser CM5"
    assert len(data["ports"]) == 8
    assert "power_watts" in data["ports"]["poe0"]
    assert data["updated_at"] == 1234567890.0


async def test_power_toggle(client):
    c, daemon = client
    resp = await c.post("/api/ports/poe2/power", json={"on": False})
    assert resp.status == 200
    assert (await resp.json())["ok"] is True
    data = await daemon._simulator.read_all()
    assert data["poe2"]["enabled"] is False


async def test_power_unknown_port_404(client):
    c, _ = client
    resp = await c.post("/api/ports/poe9/power", json={"on": True})
    assert resp.status == 404


async def test_power_injection_rejected(client):
    c, _ = client
    resp = await c.post("/api/ports/poe0%3B%20reset/power", json={"on": True})
    assert resp.status == 404


async def test_pending_command_reflected_in_status(client):
    """Until a fresh poll confirms a command, the API must report the
    commanded (target) state with pending=true — never the stale one."""
    import time as _time

    c, daemon = client
    # Simulate: latest snapshot from before the command says enabled=True
    daemon.latest_status["poe1"]["enabled"] = True
    daemon.latest_poll_started_at = _time.monotonic()
    daemon._last_command["poe1"] = (_time.monotonic() + 1, "OFF")

    data = await (await c.get("/api/status")).json()
    assert data["ports"]["poe1"]["pending"] is True
    assert data["ports"]["poe1"]["enabled"] is False  # target state
    # Other ports untouched
    assert "pending" not in data["ports"]["poe0"]


async def test_pending_clears_after_fresh_poll(client):
    import time as _time

    c, daemon = client
    daemon._last_command["poe1"] = (_time.monotonic(), "OFF")
    daemon.latest_poll_started_at = _time.monotonic() + 1  # fresh poll landed

    data = await (await c.get("/api/status")).json()
    assert "pending" not in data["ports"]["poe1"]


async def test_reset_port(client):
    c, _ = client
    resp = await c.post("/api/ports/poe1/reset")
    assert resp.status == 200
    assert (await resp.json())["reset"] is True


async def test_reset_unknown_port_404(client):
    c, _ = client
    resp = await c.post("/api/ports/poe9/reset")
    assert resp.status == 404


def test_mac_vendor_lookup():
    from exaviz_poe_mqtt_bridge.vendor_db import get_mac_vendor

    assert get_mac_vendor("00:07:5f:90:c5:16") == "VCS Video Communication Systems (Camera)"
    assert get_mac_vendor("00:13:e2:1f:bc:b9") == "GeoVision (Camera)"
    assert get_mac_vendor("00:50:56:aa:bb:cc") == "VMware Virtual"  # upstream dup resolved
    assert get_mac_vendor("d0:3b:f4:03:a6:f1") == "Unknown"
    assert get_mac_vendor(None) == "Unknown"
    assert get_mac_vendor("garbage") == "Unknown"


async def test_power_bad_body_400(client):
    c, _ = client
    for body in ({"on": "yes"}, {}, None):
        resp = await c.post("/api/ports/poe0/power", json=body)
        assert resp.status == 400
