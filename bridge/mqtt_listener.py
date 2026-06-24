import logging
import time
from typing import Callable

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

# Events dispatched to the orchestrator
CONNECTED = "connected"
DISCONNECTED = "disconnected"


class MqttListener:
    """
    Subscribes to EVCC loadpoint MQTT topics and dispatches connect/disconnect
    events to the provided callback.

    Callback signature: cb(event: str, topic: str, payload: str)
    where event is CONNECTED or DISCONNECTED.
    """

    def __init__(self, host: str, port: int, username: str, password: str,
                 topic_prefix: str, loadpoint_id: int,
                 on_event: Callable[[str], None]):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._prefix = topic_prefix
        self._lp = loadpoint_id
        self._on_event = on_event

        self._connected_topic = f"{topic_prefix}/loadpoints/{loadpoint_id}/connected"

        # Track last seen state to detect transitions (ignore retained stale state
        # on first reconnect until we see an actual transition)
        self._last_connected: bool | None = None
        self._first_message = True

        self._client = mqtt.Client(client_id="evcc-uid-bridge", clean_session=True)
        if username:
            self._client.username_pw_set(username, password)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("mqtt: connected to %s:%d", self._host, self._port)
            client.subscribe(self._connected_topic)
            log.debug("mqtt: subscribed to %s", self._connected_topic)
            self._first_message = True  # reset on reconnect — ignore retained state
        else:
            log.error("mqtt: connect failed rc=%d", rc)

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            log.warning("mqtt: unexpected disconnect rc=%d — will auto-reconnect", rc)

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        topic = msg.topic
        log.debug("mqtt: %s = %s (retained=%s)", topic, payload, msg.retain)

        if topic != self._connected_topic:
            return

        now_connected = payload.lower() == "true"

        # On the very first message after (re)connect, record state without firing
        # an event — we only want to act on genuine transitions, not stale retained
        # messages that reflect pre-restart state.
        if self._first_message:
            self._first_message = False
            self._last_connected = now_connected
            log.debug("mqtt: initial state connected=%s (no event fired)", now_connected)
            return

        if now_connected == self._last_connected:
            return  # no transition

        self._last_connected = now_connected
        event = CONNECTED if now_connected else DISCONNECTED
        log.info("mqtt: loadpoint %d → %s", self._lp, event)
        try:
            self._on_event(event)
        except Exception as exc:
            log.exception("mqtt: event handler raised: %s", exc)

    def start(self):
        """Connect and start the background network loop."""
        log.info("mqtt: connecting to %s:%d", self._host, self._port)
        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()

    def stop(self):
        self._client.loop_stop()
        self._client.disconnect()
        log.info("mqtt: disconnected")
