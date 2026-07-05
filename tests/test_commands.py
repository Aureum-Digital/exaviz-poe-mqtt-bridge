"""Tests for command validation and ESP32 command construction."""
from __future__ import annotations

import pytest

from exaviz_poe_mqtt_bridge.commands import (
    PortValidationError,
    build_port_command,
    validate_port_id,
)
from exaviz_poe_mqtt_bridge.poe import linux_port_to_esp32, parse_esp32_line

KNOWN_PORTS = [f"poe{i}" for i in range(8)]


class TestPortValidation:
    def test_valid_ports(self):
        for i in range(8):
            assert validate_port_id(f"poe{i}", KNOWN_PORTS) == i

    def test_unknown_port_rejected(self):
        with pytest.raises(PortValidationError):
            validate_port_id("poe8", KNOWN_PORTS)  # well-formed but not detected

    @pytest.mark.parametrize("bad", [
        "eth0",
        "poe",
        "poe-1",
        "poe01x",
        "poe0/../poe1",
        "poe0; reset",
        "poe0 && rm -rf /",
        "POE0",
        "poe0\n",
        "",
        "poe100",
    ])
    def test_malformed_or_injection_rejected(self, bad):
        with pytest.raises(PortValidationError):
            validate_port_id(bad, KNOWN_PORTS)


class TestEsp32Mapping:
    """Cruiser mapping from upstream: poe0-3 → PSE1, poe4-7 → PSE0."""

    @pytest.mark.parametrize("linux_port,expected", [
        (0, (1, 0)), (1, (1, 1)), (2, (1, 2)), (3, (1, 3)),
        (4, (0, 0)), (5, (0, 1)), (6, (0, 2)), (7, (0, 3)),
    ])
    def test_mapping(self, linux_port, expected):
        assert linux_port_to_esp32(linux_port) == expected


class TestBuildPortCommand:
    def test_enable_command_format(self):
        assert build_port_command("enable-port", 0) == "enable-port 1 0"
        assert build_port_command("enable-port", 7) == "enable-port 0 3"

    def test_disable_command_format(self):
        assert build_port_command("disable-port", 3) == "disable-port 1 3"
        assert build_port_command("disable-port", 4) == "disable-port 0 0"

    def test_reset_command_format(self):
        assert build_port_command("reset-port", 0) == "reset-port 1 0"
        assert build_port_command("reset-port", 6) == "reset-port 0 2"

    def test_unknown_action_rejected(self):
        with pytest.raises(ValueError):
            build_port_command("reset; rm -rf /", 0)
        with pytest.raises(ValueError):
            build_port_command("reset", 0)  # full ESP32 reboot not exposed

    def test_out_of_range_port_rejected(self):
        with pytest.raises(ValueError):
            build_port_command("enable-port", 16)
        with pytest.raises(ValueError):
            build_port_command("enable-port", -1)


class TestEsp32LineParsing:
    """Parsing of the ESP32 telemetry protocol (kept in sync with upstream)."""

    def test_powered_port(self):
        parsed = parse_esp32_line("0-0: power-on 3 15 48.500 0.325/0.800 35.2 ")
        assert parsed is not None
        assert parsed["pse_num"] == 0
        assert parsed["port_num"] == 0
        assert parsed["state"] == "power-on"
        assert parsed["class"] == "3"
        assert parsed["voltage_volts"] == 48.5
        assert parsed["current_milliamps"] == 325
        # Real power = V × I, not the class-allocation field
        assert parsed["power_watts"] == round(48.5 * 0.325, 2)
        assert parsed["enabled"] is True

    def test_disabled_port(self):
        parsed = parse_esp32_line("1-2: disabled ? 0 0.000 0.000/0.800 33.0 ")
        assert parsed is not None
        assert parsed["enabled"] is False
        assert parsed["power_watts"] == 0.0

    def test_non_port_lines_ignored(self):
        assert parse_esp32_line("0: 48.250 1250") is None  # per-PSE summary
        assert parse_esp32_line("garbage") is None
        assert parse_esp32_line("") is None
