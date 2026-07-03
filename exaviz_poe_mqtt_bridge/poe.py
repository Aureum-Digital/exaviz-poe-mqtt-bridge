"""PoE port telemetry readers for Exaviz Cruiser/Interceptor boards.

DERIVED FROM Exaviz's official Home Assistant integration (ha-poe-plugin):
  https://github.com/exavizco/ha-poe-plugin
  custom_components/exaviz/poe_readers.py
  custom_components/exaviz/board_detector.py

The parsing and detection logic below is kept as close to upstream as
practical so that fixes can flow in both directions.  Home Assistant
specific imports and the Bosch-camera tcpdump heuristics were removed;
the daemon only needs raw telemetry, link state and ARP neighbour info.

ARCHITECTURE (Cruiser carrier board):
  TPS23861 → ESP32-C6 (I2C) → CM5 (UART3) → /dev/ttyAMA3
  /dev/pse is a udev symlink to /dev/ttyAMA3 (60-pse.rules).

  ESP32 protocol, one text line per port roughly once per second:
    "{pse}-{port}: {state} {class} {power} {voltage} {current}/{limit} {temp} {error}"
    e.g. "0-0: power-on 3 15 48.500 325/800 35.2 "

  The "power" field is the PoE class allocation, NOT measured draw.
  Actual power must be computed as V × I (same as upstream).

PORT MAPPING (Cruiser, from upstream):
  Linux poe0-3 → ESP32 PSE 1, ports 0-3 (left side of board)
  Linux poe4-7 → ESP32 PSE 0, ports 0-3 (right side of board)
"""
from __future__ import annotations

import asyncio
import logging
import re
from enum import Enum
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

# PoE Class Power Allocations (IEEE 802.3) — from upstream const.py
POE_CLASS_POWER_ALLOCATION: dict[str, float] = {
    "0": 15.4,  # Class 0: Legacy/Unknown
    "1": 4.0,   # Class 1: Low power
    "2": 7.0,   # Class 2: Medium power
    "3": 15.4,  # Class 3: High power
    "4": 30.0,  # Class 4: PoE+
    "?": 15.4,  # Unknown class: assume Class 0/3 allocation
}


class BoardType(Enum):
    """Board type enumeration (from upstream board_detector.py)."""

    INTERCEPTOR = "interceptor"
    CRUISER = "cruiser"
    UNKNOWN = "unknown"


def get_allocated_power_watts(poe_class: str) -> float:
    """Get allocated power in watts based on PoE class."""
    return POE_CLASS_POWER_ALLOCATION.get(str(poe_class), 15.4)


def linux_port_to_esp32(linux_port: int) -> tuple[int, int]:
    """Convert Linux poeX number to ESP32 (pse_num, port_num).

    Linux poe0-3 → PSE 1 ports 0-3 (left side of Cruiser board)
    Linux poe4-7 → PSE 0 ports 0-3 (right side of Cruiser board)
    """
    pse_num = 1 if linux_port < 4 else 0
    pse_port = linux_port % 4
    return pse_num, pse_port


# ---------------------------------------------------------------------------
# ESP32 serial stream parsing (Cruiser) — from upstream poe_readers.py
# ---------------------------------------------------------------------------

_ESP32_PORT_PATTERN = re.compile(
    r'^(\d+)-(\d+):\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)/(\S+)\s+(\S+)\s*(.*)$'
)


def parse_esp32_line(line: str) -> dict[str, Any] | None:
    """Parse a single line from ESP32 PoE monitor output.

    Example: "0-0: power-on 3 15 48.500 325/800 35.2 "
    Returns a dict with parsed data or None if not a port line.
    """
    match = _ESP32_PORT_PATTERN.match(line.strip())
    if not match:
        return None

    (pse_num, port_num, state, poe_class, _power_str,
     voltage_str, current_str, _limit_str, temp_str, error) = match.groups()

    try:
        voltage_volts = float(voltage_str) if voltage_str != '?' else 0.0
        current_amps = float(current_str) if current_str != '?' else 0.0
        current_milliamps = int(current_amps * 1000)
        temperature_celsius = float(temp_str) if temp_str != '?' else 0.0

        # The ESP32 "power" field is the class allocation, not measured
        # consumption.  Real power = V × I (upstream does the same).
        power_watts = round(voltage_volts * current_amps, 2)

        return {
            "pse_num": int(pse_num),
            "port_num": int(port_num),
            "available": True,
            "poe_system": "onboard",
            "state": state,
            "class": poe_class,
            "power_watts": power_watts,
            "allocated_power_watts": get_allocated_power_watts(poe_class),
            "voltage_volts": round(voltage_volts, 2),
            "current_milliamps": current_milliamps,
            "temperature_celsius": round(temperature_celsius, 1),
            "enabled": state not in ("disabled",),
            "error": error.strip() if error else "",
        }
    except (ValueError, IndexError) as ex:
        _LOGGER.debug("Failed to parse ESP32 line '%s': %s", line, ex)
        return None


async def read_all_esp32_data(
    pse_device: str = "/dev/pse",
    read_seconds: float = 3.0,
) -> dict[tuple[int, int], dict[str, Any]]:
    """Read all ESP32 port data in one pass to avoid serial port conflicts.

    Reads the serial stream for `read_seconds` to capture multiple update
    cycles (the ESP32 outputs all ports roughly once per second).

    Returns a dict mapping (pse_num, port_num) to the most recent port data.
    An empty dict means the device is missing or produced no data — the
    caller should treat this as "telemetry temporarily unavailable" and
    NOT crash.
    """
    esp32_data: dict[tuple[int, int], dict[str, Any]] = {}

    # Try the configured device first, then the direct UART fallback.
    candidates = [Path(pse_device)]
    if pse_device != "/dev/ttyAMA3":
        candidates.append(Path("/dev/ttyAMA3"))

    for device_path in candidates:
        if not device_path.exists():
            continue

        try:
            _LOGGER.debug("Reading ESP32 stream from %s", device_path)

            # ESP32 outputs at 115200 baud; the udev rule sets this too,
            # but be explicit (matches upstream).
            stty_proc = await asyncio.create_subprocess_exec(
                "stty", "-F", str(device_path), "115200", "raw", "-echo",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await stty_proc.communicate()

            proc = await asyncio.create_subprocess_exec(
                "timeout", str(read_seconds), "cat", str(device_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()

            for line in stdout.decode("utf-8", errors="ignore").split("\n"):
                parsed = parse_esp32_line(line)
                if parsed:
                    esp32_data[(parsed["pse_num"], parsed["port_num"])] = parsed

            if esp32_data:
                _LOGGER.debug("Found ESP32 data for %d ports", len(esp32_data))
                break

        except Exception as ex:
            _LOGGER.warning("Failed to read ESP32 stream from %s: %s", device_path, ex)
            continue

    return esp32_data


# ---------------------------------------------------------------------------
# sysfs link state / traffic — from upstream poe_readers.py
# ---------------------------------------------------------------------------

async def _read_link_state(sys_net_path: Path) -> tuple[str, bool, int]:
    """Read link state, admin state, and speed from sysfs.

    Returns (link_state, admin_up, speed_mbps).
    """
    link_state = "unknown"
    operstate_file = sys_net_path / "operstate"
    if operstate_file.exists():
        link_state = (await asyncio.to_thread(operstate_file.read_text)).strip()

    admin_up = False
    flags_file = sys_net_path / "flags"
    if flags_file.exists():
        try:
            flags_hex = (await asyncio.to_thread(flags_file.read_text)).strip()
            admin_up = bool(int(flags_hex, 16) & 0x1)
        except (ValueError, OSError):
            admin_up = link_state in ("up", "lowerlayerdown")

    speed_mbps = 0
    if link_state == "up":
        speed_file = sys_net_path / "speed"
        if speed_file.exists():
            try:
                speed_mbps = int((await asyncio.to_thread(speed_file.read_text)).strip())
            except (ValueError, OSError):
                pass

    return link_state, admin_up, speed_mbps


async def _read_traffic_stats(sys_net_path: Path) -> tuple[int, int]:
    """Read rx_bytes and tx_bytes from sysfs statistics."""
    rx_bytes = tx_bytes = 0
    stats_path = sys_net_path / "statistics"
    if not stats_path.exists():
        return rx_bytes, tx_bytes

    for name in ("rx_bytes", "tx_bytes"):
        f = stats_path / name
        if f.exists():
            try:
                val = int((await asyncio.to_thread(f.read_text)).strip())
                if name == "rx_bytes":
                    rx_bytes = val
                else:
                    tx_bytes = val
            except (ValueError, OSError):
                pass
    return rx_bytes, tx_bytes


# ---------------------------------------------------------------------------
# Connected device via ARP/NDP — simplified from upstream (no OUI database,
# no Bosch tcpdump detection; the bridge only reports IP/MAC/hostname).
# ---------------------------------------------------------------------------

_IPV4_NEIGH = re.compile(
    r"(\d+\.\d+\.\d+\.\d+)\s+lladdr\s+([\da-f:]+).*?\b(REACHABLE|STALE|DELAY|PROBE)\b",
    re.IGNORECASE,
)
_IPV6_NEIGH = re.compile(
    r"([\da-f:]+)\s+lladdr\s+([\da-f:]+).*?\b(REACHABLE|STALE|DELAY|PROBE)\b",
    re.IGNORECASE,
)


async def _resolve_hostname(ip_address: str) -> str | None:
    """Best-effort reverse DNS lookup with a short timeout."""
    import socket

    def _lookup() -> str | None:
        try:
            return socket.gethostbyaddr(ip_address)[0]
        except (socket.herror, socket.gaierror, OSError):
            return None

    try:
        return await asyncio.wait_for(asyncio.to_thread(_lookup), timeout=2)
    except asyncio.TimeoutError:
        return None


def parse_fdb_macs(fdb_text: str, interface: str) -> list[str]:
    """Extract externally-learned MACs for a bridge port from `bridge fdb show`.

    Skips `permanent`/`self` entries (the port's own MAC) — only dynamically
    learned addresses belong to connected devices.
    """
    macs: list[str] = []
    for line in fdb_text.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0].count(":") != 5:
            continue
        if "permanent" in parts or "self" in parts:
            continue
        try:
            if parts[parts.index("dev") + 1] != interface:
                continue
        except (ValueError, IndexError):
            continue
        macs.append(parts[0].lower())
    return macs


def parse_neigh_for_macs(neigh_text: str, macs: list[str]) -> dict[str, Any] | None:
    """Find the first neighbour entry whose lladdr matches one of `macs`."""
    for line in neigh_text.splitlines():
        match = _IPV4_NEIGH.search(line) or _IPV6_NEIGH.search(line)
        if match and match.group(2).lower() in macs:
            return {
                "ip_address": match.group(1),
                "mac_address": match.group(2).lower(),
                "arp_state": match.group(3).upper(),
            }
    return None


def _bridge_master(interface: str) -> str | None:
    """Return the bridge an interface is enslaved to, or None (routed mode)."""
    master = Path(f"/sys/class/net/{interface}/master")
    try:
        return master.resolve().name if master.exists() else None
    except OSError:
        return None


async def _run(cmd: list[str]) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode() if proc.returncode == 0 else None


async def _get_connected_device_from_fdb(interface: str, bridge: str) -> dict[str, Any] | None:
    """Switch-mode detection: in a flat L2 bridge (e.g. Cruiser switch mode)
    neighbour entries live on the bridge, not the member port.  Map the port
    to its device via the bridge FDB (learned MACs per port), then look the
    MAC up in the bridge's neighbour table for its IP.
    """
    fdb_text = await _run(["bridge", "fdb", "show", "br", bridge])
    if not fdb_text:
        return None
    macs = parse_fdb_macs(fdb_text, interface)
    if not macs:
        return None

    neigh_text = await _run(["ip", "neigh", "show", "dev", bridge]) or ""
    device = parse_neigh_for_macs(neigh_text, macs)
    if device is None:
        # No IP (yet) — still report the learned MAC; better than nothing.
        return {"mac_address": macs[0]}

    hostname = await _resolve_hostname(device["ip_address"])
    if hostname:
        device["hostname"] = hostname
    return device


async def get_connected_device_from_arp(interface: str) -> dict[str, Any] | None:
    """Get connected device information from the ARP/NDP neighbour table.

    Routed mode (per-port subnets): neighbours are attached to the port.
    Switch mode (port enslaved to a bridge): fall back to FDB + bridge
    neighbour table.
    """
    try:
        bridge = _bridge_master(interface)
        if bridge:
            return await _get_connected_device_from_fdb(interface, bridge)

        proc = await asyncio.create_subprocess_exec(
            "ip", "neigh", "show", "dev", interface,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None

        output = stdout.decode().strip()
        if not output:
            return None

        match = _IPV4_NEIGH.search(output)
        if not match:
            match = _IPV6_NEIGH.search(output)
            # Validate it's actually IPv6 (multiple colons), not a MAC
            if match and match.group(1).count(":") < 2:
                match = None
        if not match:
            return None

        ip_address = match.group(1)
        device: dict[str, Any] = {
            "ip_address": ip_address,
            "mac_address": match.group(2),
            "arp_state": match.group(3).upper(),
        }
        hostname = await _resolve_hostname(ip_address)
        if hostname:
            device["hostname"] = hostname
        return device

    except Exception as ex:
        _LOGGER.debug("Failed to get ARP info for %s: %s", interface, ex)
        return None


# ---------------------------------------------------------------------------
# Per-port status assembly — from upstream read_network_port_status()
# ---------------------------------------------------------------------------

async def read_network_port_status(
    interface: str,
    esp32_data_map: dict[tuple[int, int], dict[str, Any]],
) -> dict[str, Any]:
    """Read onboard PoE port status (Cruiser): link state + ESP32 power data.

    Args:
        interface: Network interface name (e.g., "poe0")
        esp32_data_map: Pre-read ESP32 data keyed by (pse_num, port_num)
    """
    try:
        sys_net_path = Path(f"/sys/class/net/{interface}")
        if not sys_net_path.exists():
            return {"available": False, "state": "unavailable", "link_state": "down"}

        port_num = int(interface.replace("poe", ""))
        real_power_data = esp32_data_map.get(linux_port_to_esp32(port_num))

        link_state, admin_up, speed_mbps = await _read_link_state(sys_net_path)
        rx_bytes, tx_bytes = await _read_traffic_stats(sys_net_path)
        connected_device = await get_connected_device_from_arp(interface)

        if real_power_data:
            poe_class = real_power_data.get("class", "?")
            # Admin state overrides hardware state — the TPS23861 keeps
            # delivering power after `ip link set down`, but the UI should
            # show "disabled" (matches upstream).
            state = "disabled" if not admin_up else real_power_data["state"]
            return {
                "available": True,
                "poe_system": "onboard",
                "state": state,
                "link_state": link_state,
                "speed_mbps": speed_mbps,
                "rx_bytes": rx_bytes,
                "tx_bytes": tx_bytes,
                "connected_device": connected_device,
                "power_watts": real_power_data["power_watts"],
                "allocated_power_watts": get_allocated_power_watts(poe_class),
                "voltage_volts": real_power_data["voltage_volts"],
                "current_milliamps": real_power_data["current_milliamps"],
                "temperature_celsius": real_power_data.get("temperature_celsius", 0.0),
                "class": poe_class,
                "enabled": admin_up,
            }

        # ESP32 data unavailable — report network-only status with no power
        # metrics rather than upstream's mocked estimates (a bridge should
        # not publish invented wattage to HA history).
        if not admin_up:
            state = "disabled"
        elif link_state == "up":
            state = "power on"
        else:
            state = "searching"

        return {
            "available": True,
            "poe_system": "onboard",
            "state": state,
            "link_state": link_state,
            "speed_mbps": speed_mbps,
            "rx_bytes": rx_bytes,
            "tx_bytes": tx_bytes,
            "connected_device": connected_device,
            "power_watts": None,
            "allocated_power_watts": None,
            "voltage_volts": None,
            "current_milliamps": None,
            "temperature_celsius": None,
            "class": "?",
            "enabled": admin_up,
        }

    except Exception as ex:
        _LOGGER.error("Failed to read network port status %s: %s", interface, ex)
        return {"available": False, "state": "error", "error": str(ex)}


async def read_all_onboard_ports(
    interfaces: list[str],
    pse_device: str = "/dev/pse",
    serial_read_seconds: float = 3.0,
) -> dict[str, dict[str, Any]]:
    """Read all onboard PoE ports: one ESP32 serial pass + per-port sysfs/ARP."""
    esp32_data_map = await read_all_esp32_data(pse_device, serial_read_seconds)

    results = await asyncio.gather(
        *(read_network_port_status(iface, esp32_data_map) for iface in interfaces),
        return_exceptions=True,
    )

    port_data: dict[str, dict[str, Any]] = {}
    for interface, result in zip(interfaces, results):
        if isinstance(result, BaseException):
            _LOGGER.error("Failed to read interface %s: %s", interface, result)
            port_data[interface] = {"available": False, "state": "error", "error": str(result)}
        else:
            port_data[interface] = result
    return port_data


# ---------------------------------------------------------------------------
# Board / port detection — from upstream board_detector.py
# ---------------------------------------------------------------------------

async def detect_board_type() -> BoardType:
    """Detect board type using the upstream 3-tier fallback chain.

    1. /proc/device-tree/chosen/board (DT property)
    2. /boot/firmware/config.txt dtoverlay line (set by exaviz-dkms)
    3. /dev/pse (Cruiser) vs /proc/pse (Interceptor)
    """
    # Tier 1: device tree property
    board_file = Path("/proc/device-tree/chosen/board")
    try:
        if board_file.exists():
            board_name = (await asyncio.to_thread(board_file.read_text)).strip().lower()
            if board_name:
                if board_name.startswith("interceptor"):
                    return BoardType.INTERCEPTOR
                return BoardType.CRUISER
    except Exception as ex:
        _LOGGER.debug("Device tree board detection failed: %s", ex)

    # Tier 2: config.txt dtoverlay
    config_file = Path("/boot/firmware/config.txt")
    try:
        if config_file.exists():
            for line in (await asyncio.to_thread(config_file.read_text)).splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if re.match(r"dtoverlay=cruiser-", stripped):
                    return BoardType.CRUISER
                if re.match(r"dtoverlay=interceptor-", stripped):
                    return BoardType.INTERCEPTOR
    except Exception as ex:
        _LOGGER.debug("config.txt board detection failed: %s", ex)

    # Tier 3: PoE interface presence
    if Path("/dev/pse").exists() or Path("/dev/ttyAMA3").exists():
        return BoardType.CRUISER
    if Path("/proc/pse").exists():
        return BoardType.INTERCEPTOR

    _LOGGER.warning(
        "Board type could not be determined. Checked "
        "/proc/device-tree/chosen/board, /boot/firmware/config.txt, "
        "/dev/pse and /proc/pse. Is exaviz-dkms installed?"
    )
    return BoardType.UNKNOWN


async def detect_onboard_ports() -> list[str]:
    """Detect onboard PoE ports via network interfaces (poe0..poe15).

    On Cruiser boards these are real DSA ports created by the device tree
    overlay and managed by exaviz-dkms.
    """
    conf_path = Path("/proc/sys/net/ipv4/conf")
    onboard_ports: list[str] = []

    try:
        if not conf_path.exists():
            _LOGGER.debug("Network config path not found: %s", conf_path)
            return []

        # Maximum 16 onboard ports (poe0 through poe15), matches upstream.
        for i in range(16):
            if (conf_path / f"poe{i}").is_dir():
                onboard_ports.append(f"poe{i}")

        if onboard_ports:
            _LOGGER.info(
                "Detected onboard PoE ports: %s (%d ports)",
                ", ".join(onboard_ports), len(onboard_ports),
            )
        else:
            _LOGGER.warning(
                "No onboard PoE interfaces found. Verify exaviz-dkms is "
                "installed and the device tree overlay is loaded."
            )
        return sorted(onboard_ports, key=lambda p: int(p.replace("poe", "")))

    except Exception as ex:
        _LOGGER.error("Failed to detect onboard PoE ports: %s", ex)
        return []
