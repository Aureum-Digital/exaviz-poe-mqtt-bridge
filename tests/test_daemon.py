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


async def test_stale_guard_is_per_port(daemon):
    poll_started = time.monotonic()
    daemon._last_command["poe0"] = (poll_started + 1, "ON")
    daemon._publish_port("poe1", PORT_STATUS, poll_started_at=poll_started)
    topics = [t for t, _, _ in daemon.published]
    assert "exaviz/cruiser/poe/poe1/state" in topics
