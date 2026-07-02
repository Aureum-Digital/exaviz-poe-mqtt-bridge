"""PoE port control for the Exaviz Cruiser (ESP32 command interface).

Command format confirmed from the official ha-poe-plugin
(custom_components/exaviz/switch.py):

  Commands are written as text lines to /dev/pse (symlink to /dev/ttyAMA3).
  Available commands: disable-port, enable-port, reset-port, reset

  Port mapping (Linux poeX → ESP32 PSE/port):
    Linux poe0-3 → PSE 1, ports 0-3
    Linux poe4-7 → PSE 0, ports 0-3

  Disable: "disable-port <pse> <port>"  — immediately cuts PoE power,
           then `ip link set poeX down` for the interface admin state.
  Enable:  `ip link set poeX up`, then "enable-port <pse> <port>".

  ⚠️ Firmware workaround (from upstream): enable-port does not re-write
  the TPS23861 detect_class_enable register, leaving the port stuck in
  "detecting".  Upstream sends a full "reset" (ESP32 reboot) afterwards;
  power to other ports is maintained by the hardware during the reboot.
  This bridge does the same, controlled by bridge.enable_reset_workaround.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from .poe import linux_port_to_esp32

_LOGGER = logging.getLogger(__name__)

_PORT_ID_PATTERN = re.compile(r"^poe(\d{1,2})$")


class PortValidationError(Exception):
    """Raised when a port identifier is unknown or malformed."""


def validate_port_id(port_id: str, known_ports: list[str] | set[str]) -> int:
    """Validate a port identifier from an MQTT topic and return its number.

    Only strictly-formatted identifiers (poe0..poe15) that were detected
    by the board detector are accepted.  Anything else — including any
    attempt at command injection via topic segments — is rejected.
    """
    match = _PORT_ID_PATTERN.match(port_id)
    if not match:
        raise PortValidationError(f"Malformed port id: {port_id!r}")
    if port_id not in known_ports:
        raise PortValidationError(f"Unknown port: {port_id!r}")
    port_num = int(match.group(1))
    if not 0 <= port_num <= 15:
        raise PortValidationError(f"Port number out of range: {port_num}")
    return port_num


def build_port_command(action: str, linux_port: int) -> str:
    """Build a validated ESP32 command string for a Linux poe port number.

    Only "enable-port" and "disable-port" are allowed; the pse/port
    arguments are always integers derived from the validated port number,
    so no external input ever reaches the command string.
    """
    if action not in ("enable-port", "disable-port"):
        raise ValueError(f"Unsupported ESP32 action: {action!r}")
    if not 0 <= linux_port <= 15:
        raise ValueError(f"Port number out of range: {linux_port}")
    pse_num, pse_port = linux_port_to_esp32(linux_port)
    return f"{action} {pse_num} {pse_port}"


class PoEController:
    """Writes control commands to the ESP32 via /dev/pse and manages
    interface admin state via `ip link`.

    In dry-run mode no hardware or network state is touched; commands are
    only logged.
    """

    def __init__(
        self,
        pse_device: str = "/dev/pse",
        dry_run: bool = False,
        enable_reset_workaround: bool = True,
        reset_settle_seconds: float = 8.0,
    ) -> None:
        self._pse_device = pse_device
        self._dry_run = dry_run
        self._enable_reset_workaround = enable_reset_workaround
        self._reset_settle_seconds = reset_settle_seconds
        # Serialize control operations: the ESP32 UART is a single shared
        # channel and the enable workaround includes a reset + settle.
        self._lock = asyncio.Lock()

    async def _write_pse_command(self, command: str) -> bool:
        """Write a text command line to the ESP32 serial device.

        Unlike upstream (which shells out via `bash -c echo`), we write
        directly from Python — no shell is ever involved, which removes
        any injection surface.
        """
        if self._dry_run:
            _LOGGER.info("[dry-run] would send ESP32 command: %s", command)
            return True

        candidates = [Path(self._pse_device)]
        if self._pse_device != "/dev/ttyAMA3":
            candidates.append(Path("/dev/ttyAMA3"))

        for device_path in candidates:
            if not device_path.exists():
                continue
            try:
                def _write() -> None:
                    with open(device_path, "w") as dev:
                        dev.write(command + "\n")
                        dev.flush()

                await asyncio.wait_for(asyncio.to_thread(_write), timeout=5)
                _LOGGER.info("Sent ESP32 command: %s → %s", command, device_path)
                return True
            except (OSError, asyncio.TimeoutError) as exc:
                _LOGGER.warning("Could not write to %s: %s", device_path, exc)

        _LOGGER.error("No ESP32 serial device found for command: %s", command)
        return False

    async def _run_ip_link(self, interface: str, action: str) -> bool:
        """Run `ip link set <iface> up|down`. Returns True on success."""
        if self._dry_run:
            _LOGGER.info("[dry-run] would run: ip link set %s %s", interface, action)
            return True

        proc = await asyncio.create_subprocess_exec(
            "ip", "link", "set", interface, action,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            _LOGGER.error(
                "Failed to set %s %s: %s", interface, action,
                stderr.decode(errors="replace").strip(),
            )
            return False
        return True

    async def enable_port(self, linux_port: int) -> bool:
        """Enable a PoE port: link up, then restore PoE power."""
        async with self._lock:
            interface = f"poe{linux_port}"
            ok = await self._run_ip_link(interface, "up")
            ok = await self._write_pse_command(build_port_command("enable-port", linux_port)) and ok

            if self._enable_reset_workaround:
                # BANDAID (from upstream): full ESP32 reset so init()
                # re-writes detect_class_enable; other ports keep power
                # during the reboot.  Remove once firmware is fixed.
                _LOGGER.info(
                    "Sending ESP32 reset (detect_class_enable workaround), "
                    "settling %.0fs", self._reset_settle_seconds,
                )
                if await self._write_pse_command("reset") and not self._dry_run:
                    await asyncio.sleep(self._reset_settle_seconds)

            if ok:
                _LOGGER.info("Enabled PoE port %s (link up + power restored)", interface)
            return ok

    async def disable_port(self, linux_port: int) -> bool:
        """Disable a PoE port: cut PoE power first, then link down."""
        async with self._lock:
            interface = f"poe{linux_port}"
            ok = await self._write_pse_command(build_port_command("disable-port", linux_port))
            ok = await self._run_ip_link(interface, "down") and ok
            if ok:
                _LOGGER.info("Disabled PoE port %s (power cut + link down)", interface)
            return ok
