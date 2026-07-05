"""Configuration loading for the Exaviz PoE MQTT bridge.

The bridge is configured from a single YAML file, by default
/etc/exaviz-poe-mqtt-bridge/config.yaml.  See config.example.yaml.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_LOGGER = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "/etc/exaviz-poe-mqtt-bridge/config.yaml"


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


@dataclass
class MqttConfig:
    """MQTT broker connection settings."""

    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    client_id: str = "exaviz-poe-bridge"
    discovery_prefix: str = "homeassistant"
    base_topic: str = "exaviz/cruiser/poe"
    keepalive: int = 60


@dataclass
class BridgeConfig:
    """Bridge / device behaviour settings."""

    device_name: str = "Exaviz Cruiser CM5"
    device_id: str = "cruiser_cm5"
    model: str = "Cruiser CM5"
    poll_interval: float = 10.0
    pse_device: str = "/dev/pse"
    # Seconds spent reading the ESP32 serial stream per poll.  The ESP32
    # emits all ports roughly once per second; 3s captures multiple cycles.
    serial_read_seconds: float = 3.0
    # Send a full ESP32 "reset" after enable-port to work around the
    # firmware bug where detect_class_enable is not re-written (see
    # upstream ha-poe-plugin switch.py).  Disable once firmware is fixed.
    enable_reset_workaround: bool = True
    # Seconds to wait after the ESP32 reset for re-init + PoE detection.
    reset_settle_seconds: float = 8.0
    # Where UI-edited device labels are persisted (JSON, keyed by MAC).
    devices_file: str = "/var/lib/exaviz-poe-mqtt-bridge/devices.json"


@dataclass
class WebConfig:
    """Embedded web UI settings.

    The UI has no authentication — bind it to a trusted interface or
    leave it disabled.
    """

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8088


@dataclass
class DeviceLabel:
    """User-defined label/icon for a device, keyed by MAC address."""

    name: str | None = None
    icon: str | None = None  # Material Design Icons id, e.g. "mdi:cctv"


@dataclass
class Config:
    """Top-level bridge configuration."""

    mqtt: MqttConfig
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    web: WebConfig = field(default_factory=WebConfig)
    devices: dict[str, DeviceLabel] = field(default_factory=dict)


_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
_ICON_RE = re.compile(r"^mdi:[a-z0-9][a-z0-9-]*$")


def _parse_devices(section: dict[str, Any]) -> dict[str, DeviceLabel]:
    """Parse the optional `devices:` section (MAC → name/icon overrides)."""
    devices: dict[str, DeviceLabel] = {}
    for mac, spec in section.items():
        key = str(mac).strip().lower()
        if not _MAC_RE.match(key):
            _LOGGER.warning("devices: ignoring invalid MAC %r", mac)
            continue
        if not isinstance(spec, dict):
            _LOGGER.warning("devices[%s]: expected a mapping with name/icon", key)
            continue
        name = spec.get("name")
        icon = spec.get("icon")
        if icon is not None and not _ICON_RE.match(str(icon)):
            _LOGGER.warning(
                "devices[%s]: invalid icon %r (expected e.g. mdi:cctv)", key, icon
            )
            icon = None
        devices[key] = DeviceLabel(
            name=str(name) if name is not None else None,
            icon=str(icon) if icon is not None else None,
        )
    return devices


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    section = data.get(name) or {}
    if not isinstance(section, dict):
        raise ConfigError(f"Config section '{name}' must be a mapping")
    return section


def _build(cls: type, section: dict[str, Any], name: str) -> Any:
    known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    unknown = set(section) - known
    if unknown:
        _LOGGER.warning("Ignoring unknown keys in '%s' section: %s", name, ", ".join(sorted(unknown)))
    return cls(**{k: v for k, v in section.items() if k in known})


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    """Load and validate the YAML configuration file."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}. "
            "Copy config.example.yaml to that location and edit it."
        )

    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"Top level of {path} must be a mapping")

    mqtt_section = _section(data, "mqtt")
    if not mqtt_section.get("host"):
        raise ConfigError("mqtt.host is required")

    mqtt = _build(MqttConfig, mqtt_section, "mqtt")
    bridge = _build(BridgeConfig, _section(data, "bridge"), "bridge")
    web = _build(WebConfig, _section(data, "web"), "web")
    devices = _parse_devices(_section(data, "devices"))

    if bridge.poll_interval <= 0:
        raise ConfigError("bridge.poll_interval must be > 0")
    if not 1 <= web.port <= 65535:
        raise ConfigError("web.port must be a valid TCP port")

    return Config(mqtt=mqtt, bridge=bridge, web=web, devices=devices)
