"""Tests for daemon publish logic, including the stale-telemetry guard."""
from __future__ import annotations

import time

import pytest

from exaviz_poe_mqtt_bridge.config import BridgeConfig, Config, MqttConfig
from exaviz_poe_mqtt_bridge.daemon import Daemon


@pytest.fixture
async def daemon():
    config = Config(mqtt=MqttConfig(host="localhost"), bridge=BridgeConfig())
    d = Daemon(config, dry_run=True)
    d._port_ids = [f"poe{i}" for i in range(8)]
    d.published: list[tuple[str, object, bool]] = []
    d._mqtt.publish = lambda topic, payload, retain=False, qos=0: d.published.append(
        (topic, payload, retain)
    )
    return d


PORT_STATUS = {
    "available": True,
    "state": "power-on",
    "link_state": "up",
    "enabled": True,
    "power_watts": 5.2,
    "voltage_volts": 48.1,
    "current_milliamps": 108,
    "connected_device": None,
}


async def test_publish_port_publishes_state(daemon):
    daemon._publish_port("poe0", PORT_STATUS, poll_started_at=time.monotonic())
    topics = [t for t, _, _ in daemon.published]
    assert "exaviz/cruiser/poe/poe0/state" in topics


async def test_stale_poll_does_not_publish_state(daemon):
    """A poll that started before the last command must not publish state
    (it would bounce the HA switch back to the pre-command position)."""
    poll_started = time.monotonic()
    daemon._last_command["poe0"] = (poll_started + 1, "ON")  # command arrived mid-poll

    stale = dict(PORT_STATUS, enabled=False)  # pre-command snapshot
    daemon._publish_port("poe0", stale, poll_started_at=poll_started)

    topics = [t for t, _, _ in daemon.published]
    assert "exaviz/cruiser/poe/poe0/state" not in topics
    # Sensors still publish — they're harmless and refresh next cycle
    assert "exaviz/cruiser/poe/poe0/power" in topics


async def test_fresh_poll_after_command_publishes_state(daemon):
    """A poll started after the command publishes the real state again."""
    daemon._last_command["poe0"] = (time.monotonic(), "ON")
    daemon._publish_port("poe0", PORT_STATUS, poll_started_at=time.monotonic())
    topics = [t for t, _, _ in daemon.published]
    assert "exaviz/cruiser/poe/poe0/state" in topics


async def test_device_labels_applied(daemon):
    """MAC-keyed labels enrich connected devices on every poll."""
    from exaviz_poe_mqtt_bridge.config import DeviceLabel

    # Simulator's poe0 device MAC (case-insensitive match)
    daemon.device_labels = {
        "00:13:e2:1f:bc:b9": DeviceLabel(name="Camara jardin", icon="mdi:cctv"),
    }
    data = await daemon._read_ports()
    device = data["poe0"]["connected_device"]
    assert device["custom_name"] == "Camara jardin"
    assert device["icon"] == "mdi:cctv"
    # Unmapped devices untouched
    assert "custom_name" not in (data["poe3"]["connected_device"] or {})


async def test_set_device_label_persists_and_applies(daemon, tmp_path):
    import json

    daemon._devices_file = tmp_path / "devices.json"
    daemon.latest_status = await daemon._read_ports()

    daemon.set_device_label("00:13:E2:1F:BC:B9", "Camara jardin", "mdi:cctv")

    # Persisted (lowercased key)
    saved = json.loads(daemon._devices_file.read_text())
    assert saved["00:13:e2:1f:bc:b9"] == {"name": "Camara jardin", "icon": "mdi:cctv"}
    # Applied to the current snapshot immediately
    device = daemon.latest_status["poe0"]["connected_device"]
    assert device["custom_name"] == "Camara jardin"
    # And to subsequent polls
    data = await daemon._read_ports()
    assert data["poe0"]["connected_device"]["icon"] == "mdi:cctv"
    # Refresh forced so MQTT republishes soon
    assert daemon._refresh_event.is_set()


async def test_set_device_label_validation_and_removal(daemon, tmp_path):
    import pytest as _pytest

    daemon._devices_file = tmp_path / "devices.json"
    with _pytest.raises(ValueError):
        daemon.set_device_label("not-a-mac", "X", None)
    with _pytest.raises(ValueError):
        daemon.set_device_label("00:13:e2:1f:bc:b9", "X", "mdi:bad icon;")

    daemon.set_device_label("00:13:e2:1f:bc:b9", "X", None)
    assert "00:13:e2:1f:bc:b9" in daemon.device_labels
    daemon.set_device_label("00:13:e2:1f:bc:b9", "", "")  # empty removes
    assert "00:13:e2:1f:bc:b9" not in daemon.device_labels


async def test_stale_guard_is_per_port(daemon):
    poll_started = time.monotonic()
    daemon._last_command["poe0"] = (poll_started + 1, "ON")
    daemon._publish_port("poe1", PORT_STATUS, poll_started_at=poll_started)
    topics = [t for t, _, _ in daemon.published]
    assert "exaviz/cruiser/poe/poe1/state" in topics
