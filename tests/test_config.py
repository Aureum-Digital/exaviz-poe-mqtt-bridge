"""Tests for configuration parsing (devices label/icon mapping)."""
from __future__ import annotations

import pytest

from exaviz_poe_mqtt_bridge.config import ConfigError, load_config


def _write_config(tmp_path, extra: str = "") -> str:
    path = tmp_path / "config.yaml"
    path.write_text("mqtt:\n  host: localhost\n" + extra)
    return str(path)


def test_devices_parsed(tmp_path):
    config = load_config(_write_config(tmp_path, """
devices:
  "D0:3B:F4:03:A6:F1":
    name: Camara entrada
    icon: mdi:cctv
  "5c:53:10:c2:76:d2":
    name: NAS taller
"""))
    # Keys normalised to lowercase
    label = config.devices["d0:3b:f4:03:a6:f1"]
    assert label.name == "Camara entrada"
    assert label.icon == "mdi:cctv"
    assert config.devices["5c:53:10:c2:76:d2"].icon is None


def test_devices_invalid_entries_ignored(tmp_path):
    config = load_config(_write_config(tmp_path, """
devices:
  "not-a-mac":
    name: Bad
  "aa:bb:cc:dd:ee:ff": just-a-string
  "11:22:33:44:55:66":
    name: Ok
    icon: "mdi:cctv; injection"
"""))
    assert "not-a-mac" not in config.devices
    assert "aa:bb:cc:dd:ee:ff" not in config.devices
    # Invalid icon dropped, name kept
    assert config.devices["11:22:33:44:55:66"].name == "Ok"
    assert config.devices["11:22:33:44:55:66"].icon is None


def test_no_devices_section(tmp_path):
    config = load_config(_write_config(tmp_path))
    assert config.devices == {}


def test_missing_mqtt_host_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("mqtt: {}\n")
    with pytest.raises(ConfigError):
        load_config(str(path))
