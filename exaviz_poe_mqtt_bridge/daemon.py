"""Exaviz PoE MQTT bridge daemon.

Reads PoE telemetry from the host (/dev/pse + sysfs), publishes it to an
MQTT broker with Home Assistant MQTT Discovery, and executes port
enable/disable commands received over MQTT.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import signal
import sys
import time
from typing import Any

from . import __version__
from .commands import PoEController, PortValidationError, validate_port_id
from .config import Config, ConfigError, DEFAULT_CONFIG_PATH, load_config
from .discovery import build_all_discovery, port_topics
from .mqtt import MqttBridge
from .poe import (
    BoardType,
    detect_board_type,
    detect_onboard_ports,
    read_all_onboard_ports,
)

_LOGGER = logging.getLogger(__name__)

DRY_RUN_PORT_COUNT = 8


class SimulatedPoE:
    """Simulates 8 Cruiser PoE ports for development (--dry-run).

    Never touches /dev/pse or network interfaces.  Enable/disable state
    is tracked in memory so the HA switch entities behave realistically.
    """

    def __init__(self, port_count: int = DRY_RUN_PORT_COUNT) -> None:
        self.port_ids = [f"poe{i}" for i in range(port_count)]
        self._enabled = {p: True for p in self.port_ids}
        self._enabled["poe6"] = False  # one port disabled out of the box
        # Simulated connected devices; poe5 links up but draws no PoE
        # power (a non-PoE device, e.g. a laptop).
        self._devices = {
            "poe0": {"ip_address": "10.0.4.201", "mac_address": "00:13:e2:1f:bc:b9",
                     "hostname": "camera-front", "arp_state": "REACHABLE"},
            "poe3": {"ip_address": "10.0.4.204", "mac_address": "24:52:6a:08:71:80",
                     "hostname": "ap-garage", "arp_state": "REACHABLE"},
            "poe5": {"ip_address": "10.0.4.209", "mac_address": "3c:22:fb:aa:12:01",
                     "hostname": "laptop-taller", "arp_state": "REACHABLE"},
        }
        self._non_poe = {"poe5"}

    def set_enabled(self, port_id: str, enabled: bool) -> None:
        self._enabled[port_id] = enabled

    async def read_all(self) -> dict[str, dict[str, Any]]:
        data: dict[str, dict[str, Any]] = {}
        for port_id in self.port_ids:
            enabled = self._enabled[port_id]
            has_device = port_id in self._devices
            linked = enabled and has_device
            powered = linked and port_id not in self._non_poe
            voltage = round(random.uniform(47.8, 48.6), 2) if powered else 0.0
            current_ma = random.randint(120, 260) if powered else 0
            data[port_id] = {
                "available": True,
                "poe_system": "onboard",
                "state": "power-on" if powered else ("searching" if enabled else "disabled"),
                "link_state": "up" if linked else "down",
                "speed_mbps": 1000 if linked else 0,
                "rx_bytes": random.randint(10**6, 10**8) if linked else 0,
                "tx_bytes": random.randint(10**5, 10**7) if linked else 0,
                "connected_device": self._devices.get(port_id) if linked else None,
                "power_watts": round(voltage * current_ma / 1000, 2),
                "allocated_power_watts": 15.4,
                "voltage_volts": voltage,
                "current_milliamps": current_ma,
                "temperature_celsius": round(random.uniform(32.0, 38.0), 1),
                "class": "3" if has_device else "?",
                "enabled": enabled,
            }
        return data


class Daemon:
    """Main bridge daemon: telemetry loop + MQTT command handling."""

    def __init__(self, config: Config, dry_run: bool = False) -> None:
        self._config = config
        self._dry_run = dry_run
        self._loop = asyncio.get_running_loop()
        self._mqtt = MqttBridge(config.mqtt, self._loop)
        self._controller = PoEController(
            pse_device=config.bridge.pse_device,
            dry_run=dry_run,
            enable_reset_workaround=config.bridge.enable_reset_workaround,
            reset_settle_seconds=config.bridge.reset_settle_seconds,
        )
        self._simulator = SimulatedPoE() if dry_run else None
        self._port_ids: list[str] = []
        self._refresh_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        # Guard against stale-telemetry races: a poll cycle takes several
        # seconds (serial read), so a cycle that STARTED before the most
        # recent command for a port must not publish that port's state —
        # it would overwrite the optimistic state with pre-command data.
        # Maps port_id -> (monotonic timestamp, "ON"|"OFF").
        self._last_command: dict[str, tuple[float, str]] = {}
        # Latest telemetry snapshot, consumed by the web UI API.
        self.latest_status: dict[str, dict[str, Any]] = {}
        self.latest_updated: float | None = None
        # Monotonic start time of the poll that produced latest_status;
        # lets the API tell whether a command is confirmed yet.
        self.latest_poll_started_at: float = 0.0
        self.board_type: str = "unknown"

    # -- setup ---------------------------------------------------------------

    async def _detect_ports(self) -> list[str]:
        if self._simulator:
            _LOGGER.info("Dry-run mode: simulating %d PoE ports", DRY_RUN_PORT_COUNT)
            self.board_type = "cruiser (simulated)"
            return self._simulator.port_ids

        board = await detect_board_type()
        self.board_type = board.value
        _LOGGER.info("Detected board type: %s", board.value)
        if board == BoardType.INTERCEPTOR:
            _LOGGER.warning(
                "Interceptor board detected. This bridge currently implements "
                "the Cruiser (/dev/pse ESP32) path; Interceptor (/proc/pse) "
                "telemetry support is planned. Port control is unavailable."
            )
        ports = await detect_onboard_ports()
        if not ports:
            _LOGGER.error(
                "No PoE ports detected. The daemon will keep running and "
                "retry detection every poll interval."
            )
        return ports

    def _publish_discovery_and_availability(self) -> None:
        """Publish retained discovery configs + online availability.

        Called on every MQTT (re)connect so a restarted broker gets the
        retained payloads back even if it lost its persistence.
        """
        cfg = self._config
        payloads = build_all_discovery(
            discovery_prefix=cfg.mqtt.discovery_prefix,
            base_topic=cfg.mqtt.base_topic,
            device_id=cfg.bridge.device_id,
            device_name=cfg.bridge.device_name,
            model=cfg.bridge.model,
            port_ids=self._port_ids,
            version=__version__,
        )
        for topic, payload in payloads.items():
            self._mqtt.publish(topic, payload, retain=True, qos=1)
        self._mqtt.publish_online()
        _LOGGER.info(
            "Published %d retained discovery configs for %d ports",
            len(payloads), len(self._port_ids),
        )

    # -- telemetry -----------------------------------------------------------

    async def _read_ports(self) -> dict[str, dict[str, Any]]:
        if self._simulator:
            return await self._simulator.read_all()
        return await read_all_onboard_ports(
            self._port_ids,
            pse_device=self._config.bridge.pse_device,
            serial_read_seconds=self._config.bridge.serial_read_seconds,
        )

    def _publish_port(
        self, port_id: str, status: dict[str, Any], poll_started_at: float
    ) -> None:
        topics = port_topics(self._config.mqtt.base_topic, port_id)

        if not status.get("available", False):
            # Hardware temporarily unreadable: keep last retained values,
            # just log.  Bridge-level availability still says online.
            _LOGGER.warning("Port %s unavailable: %s", port_id, status.get("state"))
            return

        # Skip the state topic if this snapshot predates the last command
        # for the port — publishing it would bounce the HA switch back to
        # the pre-command state.  Sensors are harmless and refresh next
        # cycle anyway.
        if self._last_command.get(port_id, (0.0, ""))[0] > poll_started_at:
            _LOGGER.debug(
                "Skipping stale state for %s (snapshot predates command)", port_id
            )
        else:
            self._mqtt.publish(
                topics["state"], "ON" if status.get("enabled") else "OFF", retain=True
            )
        for key in ("power", "voltage", "current"):
            field = {"power": "power_watts", "voltage": "voltage_volts",
                     "current": "current_milliamps"}[key]
            value = status.get(field)
            if value is not None:
                self._mqtt.publish(topics[key], value, retain=True)

        self._mqtt.publish(
            topics["link"], "ON" if status.get("link_state") == "up" else "OFF",
            retain=True,
        )

        device = status.get("connected_device")
        if device:
            summary = device.get("hostname") or device.get("ip_address") or device.get("mac_address")
        else:
            summary = "none"
        self._mqtt.publish(topics["device"], summary, retain=True)

        attributes = {
            "state": status.get("state"),
            "poe_class": status.get("class"),
            "allocated_power_watts": status.get("allocated_power_watts"),
            "temperature_celsius": status.get("temperature_celsius"),
            "link_state": status.get("link_state"),
            "speed_mbps": status.get("speed_mbps"),
            "rx_bytes": status.get("rx_bytes"),
            "tx_bytes": status.get("tx_bytes"),
            "connected_device": device,
        }
        self._mqtt.publish(topics["attributes"], attributes, retain=True)

    async def _telemetry_loop(self) -> None:
        interval = self._config.bridge.poll_interval
        while not self._stop_event.is_set():
            try:
                if not self._port_ids:
                    # Retry detection (e.g. exaviz-dkms loaded after boot)
                    self._port_ids = await self._detect_ports()
                    if self._port_ids:
                        self._publish_discovery_and_availability()
                        self._subscribe_commands()

                if self._port_ids:
                    poll_started_at = time.monotonic()
                    data = await self._read_ports()
                    self.latest_status = data
                    self.latest_updated = time.time()
                    self.latest_poll_started_at = poll_started_at
                    for port_id, status in data.items():
                        self._publish_port(port_id, status, poll_started_at)
                    _LOGGER.debug("Published telemetry for %d ports", len(data))
            except Exception:
                _LOGGER.exception("Telemetry cycle failed; retrying next interval")

            # Wait for the poll interval, a forced refresh, or shutdown
            self._refresh_event.clear()
            waiters = {
                asyncio.create_task(self._refresh_event.wait()),
                asyncio.create_task(self._stop_event.wait()),
            }
            _, pending = await asyncio.wait(
                waiters, timeout=interval, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()

    # -- commands ------------------------------------------------------------

    def _subscribe_commands(self) -> None:
        self._mqtt.subscribe(f"{self._config.mqtt.base_topic}/+/command")

    def pending_command(self, port_id: str) -> str | None:
        """Return the commanded action ("ON"/"OFF") while it is not yet
        confirmed by a telemetry poll that started after the command."""
        ts, action = self._last_command.get(port_id, (0.0, ""))
        if ts > self.latest_poll_started_at:
            return action
        return None

    async def apply_command(self, port_id: str, action: str) -> bool:
        """Validate and execute an ON/OFF port command.

        Shared entry point for MQTT commands and the web UI.  Raises
        PortValidationError for unknown ports or unsupported actions.
        """
        action = action.upper()
        if action not in ("ON", "OFF"):
            raise PortValidationError(f"Unsupported action: {action!r}")
        port_num = validate_port_id(port_id, self._port_ids)

        _LOGGER.info("Command %s for %s", action, port_id)

        # Publish optimistic state right away (the enable path can take
        # ~10s due to the ESP32 reset workaround) and stamp the command
        # so in-flight polls with pre-command data can't bounce the
        # switch back.
        self._last_command[port_id] = (time.monotonic(), action)
        topics = port_topics(self._config.mqtt.base_topic, port_id)
        self._mqtt.publish(topics["state"], action, retain=True)

        try:
            if self._simulator:
                self._simulator.set_enabled(port_id, action == "ON")
                return True
            if action == "ON":
                return await self._controller.enable_port(port_num)
            return await self._controller.disable_port(port_num)
        finally:
            # Force a fresh poll; it starts after the command stamp, so
            # it publishes the real resulting state (also correcting the
            # optimistic value if the command failed).
            self._refresh_event.set()

    async def _command_loop(self) -> None:
        base = self._config.mqtt.base_topic
        while not self._stop_event.is_set():
            topic, payload = await self._mqtt.command_queue.get()

            # Extract port id: <base_topic>/<port_id>/command
            if not (topic.startswith(base + "/") and topic.endswith("/command")):
                _LOGGER.warning("Ignoring message on unexpected topic: %s", topic)
                continue
            port_id = topic[len(base) + 1 : -len("/command")]

            try:
                ok = await self.apply_command(port_id, payload)
                if not ok:
                    _LOGGER.error("Command %s for %s failed", payload, port_id)
            except PortValidationError as exc:
                _LOGGER.warning("Rejected command: %s", exc)
            except Exception:
                _LOGGER.exception("Command %s for %s raised", payload, port_id)

    # -- lifecycle -----------------------------------------------------------

    async def run(self) -> int:
        self._port_ids = await self._detect_ports()

        # Re-publish discovery + availability + subscriptions on every
        # (re)connect; runs in the paho thread, publish() is thread-safe.
        self._mqtt.set_on_connect(self._publish_discovery_and_availability)
        if self._port_ids:
            self._subscribe_commands()

        await self._mqtt.connect()

        web_server = None
        if self._config.web.enabled:
            from .web import WebServer

            web_server = WebServer(self, self._config)
            await web_server.start()

        tasks = [
            asyncio.create_task(self._telemetry_loop(), name="telemetry"),
            asyncio.create_task(self._command_loop(), name="commands"),
        ]

        stop = asyncio.create_task(self._stop_event.wait())
        await asyncio.wait([stop], return_when=asyncio.FIRST_COMPLETED)

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if web_server:
            await web_server.stop()
        await self._mqtt.disconnect()
        _LOGGER.info("Bridge stopped cleanly")
        return 0

    def request_stop(self) -> None:
        self._stop_event.set()
        self._refresh_event.set()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )


async def _async_main(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        _LOGGER.error("%s", exc)
        return 2

    daemon = Daemon(config, dry_run=args.dry_run)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, daemon.request_stop)

    return await daemon.run()


def main() -> None:
    """Console entry point."""
    parser = argparse.ArgumentParser(
        prog="exaviz-poe-mqtt-bridge",
        description="Expose Exaviz Cruiser PoE ports to Home Assistant over MQTT",
    )
    parser.add_argument(
        "--config", "-c", default=DEFAULT_CONFIG_PATH,
        help=f"Path to YAML config (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate 8 PoE ports without touching /dev/pse or ip link",
    )
    parser.add_argument(
        "--log-level", default="info",
        choices=["debug", "info", "warning", "error"],
        help="Log verbosity (default: info)",
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()

    _setup_logging(args.log_level)
    _LOGGER.info("exaviz-poe-mqtt-bridge %s starting%s",
                 __version__, " (dry-run)" if args.dry_run else "")

    sys.exit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
