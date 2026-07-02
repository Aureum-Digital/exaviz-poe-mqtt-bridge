"""Tests for MQTT Discovery payload generation."""
from __future__ import annotations

from exaviz_poe_mqtt_bridge.discovery import (
    availability_topic,
    build_all_discovery,
    build_port_discovery,
    port_topics,
)

KW = dict(
    discovery_prefix="homeassistant",
    base_topic="exaviz/cruiser/poe",
    device_id="cruiser_cm5",
    device_name="Exaviz Cruiser CM5",
    model="Cruiser CM5",
    version="0.1.0",
)


def test_port_topics_layout():
    topics = port_topics("exaviz/cruiser/poe", "poe0")
    assert topics["state"] == "exaviz/cruiser/poe/poe0/state"
    assert topics["command"] == "exaviz/cruiser/poe/poe0/command"
    assert topics["power"] == "exaviz/cruiser/poe/poe0/power"
    assert topics["voltage"] == "exaviz/cruiser/poe/poe0/voltage"
    assert topics["current"] == "exaviz/cruiser/poe/poe0/current"
    assert topics["link"] == "exaviz/cruiser/poe/poe0/link"
    assert topics["device"] == "exaviz/cruiser/poe/poe0/device"


def test_availability_topic():
    assert availability_topic("exaviz/cruiser/poe") == "exaviz/cruiser/poe/availability"


def test_switch_payload():
    payloads = build_port_discovery(port_id="poe0", **KW)
    topic = "homeassistant/switch/exaviz_cruiser_cm5_poe0_power/config"
    assert topic in payloads
    switch = payloads[topic]
    assert switch["state_topic"] == "exaviz/cruiser/poe/poe0/state"
    assert switch["command_topic"] == "exaviz/cruiser/poe/poe0/command"
    assert switch["payload_on"] == "ON"
    assert switch["payload_off"] == "OFF"
    assert switch["availability_topic"] == "exaviz/cruiser/poe/availability"
    assert switch["unique_id"] == "exaviz_cruiser_cm5_poe0_switch"


def test_shared_device_block():
    payloads = build_port_discovery(port_id="poe3", **KW)
    devices = [p["device"] for p in payloads.values()]
    assert all(d == devices[0] for d in devices)
    device = devices[0]
    assert device["identifiers"] == ["exaviz_cruiser_cm5"]
    assert device["manufacturer"] == "Exaviz"
    assert device["name"] == "Exaviz Cruiser CM5"
    assert device["model"] == "Cruiser CM5"


def test_entity_components_per_port():
    payloads = build_port_discovery(port_id="poe5", **KW)
    components = sorted(t.split("/")[1] for t in payloads)
    assert components == sorted(
        ["switch", "sensor", "sensor", "sensor", "sensor", "binary_sensor"]
    )
    # power/voltage/current sensors carry proper units and classes
    power = payloads["homeassistant/sensor/exaviz_cruiser_cm5_poe5_power/config"]
    assert power["unit_of_measurement"] == "W"
    assert power["device_class"] == "power"
    assert power["state_class"] == "measurement"
    voltage = payloads["homeassistant/sensor/exaviz_cruiser_cm5_poe5_voltage/config"]
    assert voltage["unit_of_measurement"] == "V"
    current = payloads["homeassistant/sensor/exaviz_cruiser_cm5_poe5_current/config"]
    assert current["unit_of_measurement"] == "mA"
    link = payloads["homeassistant/binary_sensor/exaviz_cruiser_cm5_poe5_link/config"]
    assert link["device_class"] == "connectivity"
    assert link["state_topic"] == "exaviz/cruiser/poe/poe5/link"


def test_unique_ids_are_unique_across_ports():
    payloads = build_all_discovery(port_ids=[f"poe{i}" for i in range(8)], **KW)
    unique_ids = [p["unique_id"] for p in payloads.values()]
    assert len(unique_ids) == len(set(unique_ids))
    assert len(payloads) == 8 * 6  # 6 entities per port


def test_all_payloads_have_availability():
    payloads = build_all_discovery(port_ids=["poe0", "poe1"], **KW)
    for payload in payloads.values():
        assert payload["availability_topic"] == "exaviz/cruiser/poe/availability"
        assert payload["payload_available"] == "online"
        assert payload["payload_not_available"] == "offline"
