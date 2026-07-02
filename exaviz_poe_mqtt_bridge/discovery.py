"""Home Assistant MQTT Discovery payload generation.

One retained config payload per entity, per port:
  switch          — PoE power enable/disable
  sensor          — power draw (W), voltage (V), current (mA)
  binary_sensor   — link/connectivity state
  sensor          — connected device summary (+ JSON attributes)

All entities share a single device block and a single bridge-level
availability topic backed by the MQTT Last Will.
"""
from __future__ import annotations

from typing import Any


def availability_topic(base_topic: str) -> str:
    """Bridge-level availability topic (retained online/offline + LWT)."""
    return f"{base_topic}/availability"


def port_topics(base_topic: str, port_id: str) -> dict[str, str]:
    """State/command topics for one port (e.g. port_id='poe0')."""
    prefix = f"{base_topic}/{port_id}"
    return {
        "state": f"{prefix}/state",
        "command": f"{prefix}/command",
        "power": f"{prefix}/power",
        "voltage": f"{prefix}/voltage",
        "current": f"{prefix}/current",
        "link": f"{prefix}/link",
        "device": f"{prefix}/device",
        "attributes": f"{prefix}/attributes",
    }


def device_info(device_id: str, device_name: str, model: str, version: str) -> dict[str, Any]:
    """Shared HA device block for all entities."""
    return {
        "identifiers": [f"exaviz_{device_id}"],
        "manufacturer": "Exaviz",
        "name": device_name,
        "model": model,
        "sw_version": f"exaviz-poe-mqtt-bridge {version}",
    }


def _port_label(port_id: str) -> str:
    """Human-friendly port label: poe0 → 'Port 1' (P1 on the board)."""
    try:
        return f"Port {int(port_id.replace('poe', '')) + 1}"
    except ValueError:
        return port_id


def build_port_discovery(
    *,
    discovery_prefix: str,
    base_topic: str,
    device_id: str,
    device_name: str,
    model: str,
    port_id: str,
    version: str = "0.0.0",
) -> dict[str, dict[str, Any]]:
    """Build all discovery payloads for one PoE port.

    Returns a mapping of discovery config topic → payload dict.  All
    payloads must be published retained.
    """
    topics = port_topics(base_topic, port_id)
    avail = availability_topic(base_topic)
    device = device_info(device_id, device_name, model, version)
    label = _port_label(port_id)
    node = f"exaviz_{device_id}_{port_id}"

    common = {
        "availability_topic": avail,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device,
    }

    payloads: dict[str, dict[str, Any]] = {}

    payloads[f"{discovery_prefix}/switch/{node}_power/config"] = {
        **common,
        "name": f"{label} PoE Power",
        "unique_id": f"{node}_switch",
        "state_topic": topics["state"],
        "command_topic": topics["command"],
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "outlet",
        "icon": "mdi:ethernet",
    }

    payloads[f"{discovery_prefix}/sensor/{node}_power/config"] = {
        **common,
        "name": f"{label} Power",
        "unique_id": f"{node}_power",
        "state_topic": topics["power"],
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "suggested_display_precision": 1,
    }

    payloads[f"{discovery_prefix}/sensor/{node}_voltage/config"] = {
        **common,
        "name": f"{label} Voltage",
        "unique_id": f"{node}_voltage",
        "state_topic": topics["voltage"],
        "unit_of_measurement": "V",
        "device_class": "voltage",
        "state_class": "measurement",
        "suggested_display_precision": 1,
        "entity_category": "diagnostic",
    }

    payloads[f"{discovery_prefix}/sensor/{node}_current/config"] = {
        **common,
        "name": f"{label} Current",
        "unique_id": f"{node}_current",
        "state_topic": topics["current"],
        "unit_of_measurement": "mA",
        "device_class": "current",
        "state_class": "measurement",
        "suggested_display_precision": 0,
        "entity_category": "diagnostic",
    }

    payloads[f"{discovery_prefix}/binary_sensor/{node}_link/config"] = {
        **common,
        "name": f"{label} Link",
        "unique_id": f"{node}_link",
        "state_topic": topics["link"],
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "connectivity",
    }

    payloads[f"{discovery_prefix}/sensor/{node}_device/config"] = {
        **common,
        "name": f"{label} Connected Device",
        "unique_id": f"{node}_device",
        "state_topic": topics["device"],
        "json_attributes_topic": topics["attributes"],
        "icon": "mdi:lan-connect",
    }

    return payloads


def build_all_discovery(
    *,
    discovery_prefix: str,
    base_topic: str,
    device_id: str,
    device_name: str,
    model: str,
    port_ids: list[str],
    version: str = "0.0.0",
) -> dict[str, dict[str, Any]]:
    """Build discovery payloads for all detected ports."""
    payloads: dict[str, dict[str, Any]] = {}
    for port_id in port_ids:
        payloads.update(
            build_port_discovery(
                discovery_prefix=discovery_prefix,
                base_topic=base_topic,
                device_id=device_id,
                device_name=device_name,
                model=model,
                port_id=port_id,
                version=version,
            )
        )
    return payloads
