"""Embedded web UI for the Exaviz PoE MQTT bridge.

A single static page (no build step) served by aiohttp, plus a small
JSON API.  Enabled via the `web:` config section.

  GET  /                          — the UI
  GET  /api/status                — device info + latest port telemetry
  POST /api/ports/{port_id}/power — body {"on": true|false}

NOTE: the API has no authentication; it exposes the same control surface
as the MQTT command topics.  Bind it to a trusted interface.
"""
from __future__ import annotations

import logging
from importlib import resources
from typing import TYPE_CHECKING, Any

from aiohttp import web

from . import __version__
from .commands import PortValidationError

if TYPE_CHECKING:
    from .config import Config
    from .daemon import Daemon

_LOGGER = logging.getLogger(__name__)


def _status_payload(daemon: "Daemon", config: "Config") -> dict[str, Any]:
    # While a command awaits confirmation from a fresh telemetry poll,
    # report the commanded (target) state and flag the port as pending —
    # otherwise the UI would flash the stale pre-command state.
    ports: dict[str, Any] = {}
    for port_id, status in daemon.latest_status.items():
        action = daemon.pending_command(port_id)
        if action:
            status = {**status, "pending": True, "enabled": action == "ON"}
        ports[port_id] = status
    return {
        "device": {
            "name": config.bridge.device_name,
            "id": config.bridge.device_id,
            "model": config.bridge.model,
            "board_type": daemon.board_type,
            "version": __version__,
        },
        "poll_interval": config.bridge.poll_interval,
        "updated_at": daemon.latest_updated,
        "ports": ports,
    }


class WebServer:
    """aiohttp server bound to the daemon's state and command path."""

    def __init__(self, daemon: "Daemon", config: "Config") -> None:
        self._daemon = daemon
        self._config = config
        self._runner: web.AppRunner | None = None
        self._index_html = (
            resources.files("exaviz_poe_mqtt_bridge") / "static" / "index.html"
        ).read_text()

    async def _handle_index(self, request: web.Request) -> web.Response:
        return web.Response(text=self._index_html, content_type="text/html")

    async def _handle_status(self, request: web.Request) -> web.Response:
        return web.json_response(_status_payload(self._daemon, self._config))

    async def _handle_power(self, request: web.Request) -> web.Response:
        port_id = request.match_info["port_id"]
        try:
            body = await request.json()
            on = body["on"]
            if not isinstance(on, bool):
                raise ValueError
        except Exception:
            return web.json_response(
                {"error": 'body must be {"on": true|false}'}, status=400
            )

        try:
            ok = await self._daemon.apply_command(port_id, "ON" if on else "OFF")
        except PortValidationError as exc:
            return web.json_response({"error": str(exc)}, status=404)

        if not ok:
            return web.json_response(
                {"error": "command failed, check daemon logs"}, status=502
            )
        return web.json_response({"ok": True, "port": port_id, "on": on})

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/api/status", self._handle_status)
        app.router.add_post("/api/ports/{port_id}/power", self._handle_power)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._config.web.host, self._config.web.port)
        await site.start()
        _LOGGER.info(
            "Web UI listening on http://%s:%d (no auth — trusted networks only)",
            self._config.web.host, self._config.web.port,
        )

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
