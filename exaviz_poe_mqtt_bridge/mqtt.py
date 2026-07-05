"""Thin asyncio-friendly wrapper around paho-mqtt.

paho runs its network loop in a background thread (loop_start); incoming
command messages are handed to the asyncio side via call_soon_threadsafe
into an asyncio.Queue.  Reconnects are handled by paho automatically;
on every (re)connect we re-subscribe and invoke the on_connect callback
so the daemon can re-publish discovery + availability.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

import paho.mqtt.client as mqtt

from .config import MqttConfig
from .discovery import availability_topic

_LOGGER = logging.getLogger(__name__)


class MqttBridge:
    """MQTT connection with LWT, reconnect and a command queue."""

    def __init__(self, config: MqttConfig, loop: asyncio.AbstractEventLoop) -> None:
        self._config = config
        self._loop = loop
        self.command_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._on_connect_cb: Callable[[], None] | None = None
        self._subscriptions: list[str] = []

        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=config.client_id,
            protocol=mqtt.MQTTv311,
        )
        if config.username:
            self._client.username_pw_set(config.username, config.password)

        # Last will: mark the whole bridge offline if we die unexpectedly.
        self._availability_topic = availability_topic(config.base_topic)
        self._client.will_set(self._availability_topic, "offline", qos=1, retain=True)

        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._client.on_connect = self._handle_connect
        self._client.on_connect_fail = self._handle_connect_fail
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._handle_message

    # -- lifecycle ----------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Whether the client currently holds a broker connection."""
        return self._client.is_connected()

    def set_on_connect(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked (from the paho thread) on every
        successful (re)connect, after subscriptions are restored."""
        self._on_connect_cb = callback

    async def connect(self) -> None:
        """Connect and start the paho network thread.

        connect_async + loop_start keeps retrying in the background, so
        the daemon starts fine even if the broker is briefly down.
        """
        self._client.connect_async(
            self._config.host, self._config.port, keepalive=self._config.keepalive
        )
        self._client.loop_start()

    async def disconnect(self) -> None:
        """Publish offline availability and disconnect cleanly."""
        try:
            info = self._client.publish(self._availability_topic, "offline", qos=1, retain=True)
            info.wait_for_publish(timeout=5)
        except Exception:
            pass
        self._client.disconnect()
        self._client.loop_stop()

    # -- publishing ---------------------------------------------------------

    def publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
        """Publish a payload (dicts are JSON-encoded, None becomes empty)."""
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        elif payload is None:
            payload = ""
        elif not isinstance(payload, (str, bytes, int, float)):
            payload = str(payload)
        self._client.publish(topic, payload, qos=qos, retain=retain)

    def publish_online(self) -> None:
        """Publish retained 'online' availability."""
        self.publish(self._availability_topic, "online", retain=True, qos=1)

    # -- subscriptions ------------------------------------------------------

    def subscribe(self, topic_filter: str) -> None:
        """Subscribe (also restored automatically after reconnect)."""
        self._subscriptions.append(topic_filter)
        self._client.subscribe(topic_filter, qos=1)

    # -- paho callbacks (run in the paho network thread) ---------------------

    def _handle_connect(self, client: mqtt.Client, userdata: Any, flags: Any,
                        reason_code: Any, properties: Any = None) -> None:
        if reason_code.is_failure:
            _LOGGER.error("MQTT connect failed: %s", reason_code)
            return
        _LOGGER.info(
            "Connected to MQTT broker %s:%d", self._config.host, self._config.port
        )
        for topic_filter in self._subscriptions:
            client.subscribe(topic_filter, qos=1)
        if self._on_connect_cb:
            self._on_connect_cb()

    def _handle_connect_fail(self, client: mqtt.Client, userdata: Any) -> None:
        _LOGGER.warning(
            "Cannot reach MQTT broker %s:%d (TCP connect failed), retrying",
            self._config.host, self._config.port,
        )

    def _handle_disconnect(self, client: mqtt.Client, userdata: Any,
                           disconnect_flags: Any, reason_code: Any,
                           properties: Any = None) -> None:
        if reason_code != 0:
            _LOGGER.warning("MQTT disconnected (%s), will auto-reconnect", reason_code)

    def _handle_message(self, client: mqtt.Client, userdata: Any,
                        msg: mqtt.MQTTMessage) -> None:
        payload = msg.payload.decode(errors="replace").strip()
        _LOGGER.debug("MQTT message: %s = %s", msg.topic, payload)
        self._loop.call_soon_threadsafe(
            self.command_queue.put_nowait, (msg.topic, payload)
        )
