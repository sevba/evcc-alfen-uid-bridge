"""
Orchestrator integration tests with stubbed Alfen and EVCC clients.
Exercises connect/disconnect transitions and the dry-run path.
"""
import queue
import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from bridge.config import Config, normalise_uid
from bridge.orchestrator import Orchestrator


def _make_config(**overrides):
    cfg = Config()
    cfg.alfen_host = "10.0.40.66"
    cfg.alfen_username = "admin"
    cfg.alfen_password = "test"
    cfg.alfen_socket = 1
    cfg.alfen_tls_verify = False
    cfg.tag_wait_timeout = 6
    cfg.tag_poll_interval = 1
    cfg.login_rate_max = 5
    cfg.login_rate_window = 60
    cfg.evcc_base_url = "http://127.0.0.1:7070"
    cfg.evcc_loadpoint_id = 1
    cfg.release_on_disconnect = True
    cfg.on_unknown_tag = "auto"
    cfg.default_vehicle = ""
    cfg.mqtt_host = "127.0.0.1"
    cfg.mqtt_port = 1883
    cfg.mqtt_username = ""
    cfg.mqtt_password = ""
    cfg.mqtt_topic_prefix = "evcc"
    cfg.uid_vehicle_map = {normalise_uid("04A1B2C3"): "bmw320e"}
    cfg.dry_run = False
    cfg.log_level = "DEBUG"
    cfg.log_uid_plaintext = True
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _run_event(cfg, event, alfen_uid=None):
    """
    Run the orchestrator event handler directly (bypassing MQTT).
    Returns the last call made on the EVCC client.
    """
    evcc_calls = []

    with patch("bridge.orchestrator.AlfenClient") as MockAlfen, \
         patch("bridge.orchestrator.EvccClient") as MockEvcc, \
         patch("bridge.orchestrator.MqttListener"):

        mock_alfen_inst = MagicMock()
        mock_alfen_inst.login.return_value = True
        mock_alfen_inst.get_latest_tag.return_value = alfen_uid
        MockAlfen.return_value = mock_alfen_inst

        mock_evcc_inst = MagicMock()
        mock_evcc_inst.set_vehicle.return_value = True
        mock_evcc_inst.clear_vehicle.return_value = True
        mock_evcc_inst.get_vehicle.return_value = ""
        MockEvcc.return_value = mock_evcc_inst

        orch = Orchestrator(cfg)
        orch._evcc = mock_evcc_inst

        if event == "connected":
            orch._handle_connect(datetime.now(tz=timezone.utc))
        elif event == "disconnected":
            orch._handle_disconnect()

        return mock_evcc_inst


def test_known_uid_sets_vehicle():
    cfg = _make_config()
    evcc = _run_event(cfg, "connected", alfen_uid="04A1B2C3")
    evcc.set_vehicle.assert_called_once_with("bmw320e")


def test_unknown_uid_auto_mode_no_action():
    cfg = _make_config(on_unknown_tag="auto")
    evcc = _run_event(cfg, "connected", alfen_uid="DEADBEEF")
    evcc.set_vehicle.assert_not_called()


def test_unknown_uid_default_mode_sets_default():
    cfg = _make_config(on_unknown_tag="default", default_vehicle="bmw320e")
    evcc = _run_event(cfg, "connected", alfen_uid="DEADBEEF")
    evcc.set_vehicle.assert_called_once_with("bmw320e")


def test_no_tag_found_auto_mode_no_action():
    cfg = _make_config(tag_wait_timeout=2, tag_poll_interval=1)
    evcc = _run_event(cfg, "connected", alfen_uid=None)
    evcc.set_vehicle.assert_not_called()


def test_disconnect_releases_vehicle():
    cfg = _make_config(release_on_disconnect=True)
    evcc = _run_event(cfg, "disconnected")
    evcc.clear_vehicle.assert_called_once()


def test_disconnect_no_release_when_disabled():
    cfg = _make_config(release_on_disconnect=False)
    evcc = _run_event(cfg, "disconnected")
    evcc.clear_vehicle.assert_not_called()


def test_idempotency_no_duplicate_set():
    cfg = _make_config()
    with patch("bridge.orchestrator.AlfenClient") as MockAlfen, \
         patch("bridge.orchestrator.EvccClient") as MockEvcc, \
         patch("bridge.orchestrator.MqttListener"):

        mock_alfen = MagicMock()
        mock_alfen.login.return_value = True
        mock_alfen.get_latest_tag.return_value = "04A1B2C3"
        MockAlfen.return_value = mock_alfen

        mock_evcc = MagicMock()
        mock_evcc.set_vehicle.return_value = True
        MockEvcc.return_value = mock_evcc

        orch = Orchestrator(cfg)
        orch._evcc = mock_evcc

        ts = datetime.now(tz=timezone.utc)
        orch._handle_connect(ts)
        orch._handle_connect(ts)  # second connect with same vehicle

    assert mock_evcc.set_vehicle.call_count == 1  # only once


def test_dry_run_does_not_call_evcc():
    cfg = _make_config(dry_run=True)
    with patch("bridge.orchestrator.AlfenClient") as MockAlfen, \
         patch("bridge.orchestrator.MqttListener"):

        mock_alfen = MagicMock()
        mock_alfen.login.return_value = True
        mock_alfen.get_latest_tag.return_value = "04A1B2C3"
        MockAlfen.return_value = mock_alfen

        # Use a real EvccClient in dry_run mode — it logs but doesn't HTTP
        from bridge.evcc_client import EvccClient
        real_evcc = EvccClient("http://127.0.0.1:7070", 1, dry_run=True)

        with patch("requests.post") as mock_post, patch("requests.delete") as mock_del:
            orch = Orchestrator(cfg)
            orch._evcc = real_evcc
            orch._handle_connect(datetime.now(tz=timezone.utc))
            mock_post.assert_not_called()
            mock_del.assert_not_called()
