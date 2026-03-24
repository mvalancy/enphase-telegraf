"""Enlighten live stream client — MQTT over WebSocket with protobuf.

First public implementation of the Enphase Enlighten real-time
streaming protocol. Connects to AWS IoT MQTT broker and receives
protobuf-encoded DataMsg messages with per-phase power for all meters.

Timing constants reverse-engineered from the Enlighten React app
(references/enlighten-react-app/js/main-78975918.adb367c7.js):
  - MQTT keepAlive: 60s (Paho default, app never overrides)
  - Data staleness timeout: 10s (this.timeOut = 1e4)
  - log_live_status: analytics POST, NOT a heartbeat
  - Session duration: 900s (15 min), auto-reconnect at 840s (14 min)
  - maxMqttResponseCount: 3 (some views disconnect after 3 messages)

Usage:
    client = EnlightenClient(email, password)
    client.login()
    stream = LiveStreamClient(client)
    stream.start(serial, on_data=lambda d: print(d))
    # ... receives ~1 message/second with full system state
    stream.stop()
"""

import json
import logging
import ssl
import sys
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote

log = logging.getLogger(__name__)

# Load compiled protobuf — check multiple locations
_PROTO_SEARCH_PATHS = [
    Path(__file__).parent / "proto",                                 # package: enphase_cloud/proto/
    Path(__file__).parent.parent.parent.parent / "data" / "proto",  # dev: repo root
    Path("/app/data/proto"),                                         # Docker container
]
_proto_available = False
DataMsg_pb2 = None
MeterSummaryData_pb2 = None


def _load_proto(extra_paths: list[Path] | None = None):
    """Load compiled protobuf schemas. Called automatically on import.

    Users can call this manually with extra paths if protos are in a custom location:
        from enphase_cloud.livestream import _load_proto
        _load_proto([Path("/my/proto/dir")])
    """
    global _proto_available, DataMsg_pb2, MeterSummaryData_pb2
    if _proto_available:
        return True
    search = list(extra_paths or []) + _PROTO_SEARCH_PATHS
    for _pdir in search:
        if (_pdir / "DataMsg_pb2.py").exists():
            sys.path.insert(0, str(_pdir))
            try:
                import DataMsg_pb2 as _dm_pb2
                import MeterSummaryData_pb2 as _ms_pb2
                DataMsg_pb2 = _dm_pb2
                MeterSummaryData_pb2 = _ms_pb2
                _proto_available = True
                return True
            except ImportError:
                pass
    return False


_load_proto()
if not _proto_available:
    log.warning("Protobuf schemas not found — MQTT live stream will be unavailable")

# ── Timing constants (from React app analysis) ──────────
# The React app uses Paho's default keepAlive of 60s.
# Data flows purely from the MQTT subscription — no polling needed.
# log_live_status is an analytics POST, not a data trigger.
MQTT_KEEPALIVE_S = 60
SESSION_DURATION_S = 840  # reconnect at 14 min (session is 15 min)
SESSION_GAP_S = 2         # pause between sessions
CONNECT_TIMEOUT_S = 10    # wait for connection
RECONNECT_DELAY_S = 30    # wait after failed connection


class LiveStreamClient:
    """MQTT client for Enlighten real-time protobuf data stream.

    Matches the behavior of the Enlighten React app:
    - Connects via WebSocket to AWS IoT with custom authorizer
    - Subscribes to live_stream and response_stream topics
    - Receives protobuf-encoded DataMsg at ~1Hz
    - Auto-reconnects every 14 minutes (session TTL is 15 min)
    - No heartbeat polling — data flows from MQTT subscription alone
    """

    def __init__(self, enlighten_client):
        self.enlighten = enlighten_client
        self._mqtt_client = None
        self._connected = False
        self._running = False
        self._serial = None
        self._on_data: Callable | None = None
        self._on_status: Callable | None = None
        self._reconnect_thread: threading.Thread | None = None
        self._message_count = 0
        self._last_message_time = 0.0
        self._session_message_count = 0
        self._topics = {}
        self._creds = {}
        self._last_decoded = {}

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def stats(self) -> dict:
        return {
            "connected": self._connected,
            "running": self._running,
            "messages": self._message_count,
            "session_messages": self._session_message_count,
            "last_message": self._last_message_time,
            "topics": list(self._topics.keys()),
            "proto_available": _proto_available,
            "last_data": self._last_decoded,
        }

    def start(self, serial: str, on_data: Callable = None, on_status: Callable = None):
        """Start streaming. Connects, subscribes, and auto-reconnects.

        The stream session lasts 15 minutes. This client automatically
        refreshes credentials and reconnects before expiry.
        """
        if not _proto_available:
            raise RuntimeError("Protobuf schemas not compiled — run setup.sh")

        self._serial = serial
        self._on_data = on_data
        self._on_status = on_status
        self._running = True

        self._reconnect_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._reconnect_thread.start()

    def stop(self):
        self._running = False
        self._disconnect()

    def _run_loop(self):
        """Main loop: connect, stream for 14 min, reconnect."""
        while self._running:
            try:
                self._connect()

                # Wait for connection to establish
                for _ in range(CONNECT_TIMEOUT_S):
                    if self._connected:
                        break
                    time.sleep(1)

                if not self._connected:
                    self._status("Connection failed — retrying in 30s")
                    self._disconnect()
                    time.sleep(RECONNECT_DELAY_S)
                    continue

                # Stream for 14 minutes (session is 15 min)
                session_start = time.time()
                while self._running and self._connected:
                    elapsed = time.time() - session_start
                    if elapsed > SESSION_DURATION_S:
                        break
                    time.sleep(1)

                self._disconnect()
                if self._running:
                    self._status("Session expired, reconnecting...")
                    time.sleep(SESSION_GAP_S)

            except Exception as e:
                log.exception("Stream error: %s", e)
                self._status(f"Error: {e}")
                self._disconnect()
                if self._running:
                    time.sleep(RECONNECT_DELAY_S)

    def _connect(self):
        import paho.mqtt.client as mqtt

        self.enlighten._ensure_auth()
        self._status("Fetching credentials...")

        # Get live stream credentials
        creds = self._get_stream_credentials(self._serial)
        if not creds.get("aws_iot_endpoint"):
            raise RuntimeError("Could not get stream credentials")

        site_id = self.enlighten._session.site_id
        resp_creds = self._get_response_credentials(site_id)

        endpoint = creds["aws_iot_endpoint"]
        authorizer = creds.get("aws_authorizer", "aws-lambda-authoriser-prod")
        token_key = creds.get("aws_token_key", "enph_token")
        token_value = creds.get("aws_token_value", "")
        digest = creds.get("aws_digest", "")
        live_topic = creds.get("live_stream_topic", "")
        resp_topic = resp_creds.get("topic", "")

        self._creds = creds
        self._topics = {}
        if live_topic:
            self._topics["live_stream"] = live_topic
        if resp_topic:
            self._topics["response_stream"] = resp_topic

        # Client ID format matches React app: em-paho-mqtt-{nanotime}
        # React uses moment().valueOf() which is ms since epoch
        client_id = f"em-paho-mqtt-{int(time.time() * 1000)}"
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            transport="websockets",
        )

        # Auth in username field — exact format from React app
        # (Paho Python's WebSocket transport doesn't pass URL query params correctly)
        username = (
            f"?x-amz-customauthorizer-name={authorizer}"
            f"&{token_key}={token_value}"
            f"&site-id={site_id}"
            f"&x-amz-customauthorizer-signature={quote(digest)}"
        )
        client.username_pw_set(username)
        client.tls_set()

        # React app uses reconnect:true and default keepAlive (60s)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

        self._status(f"Connecting to {endpoint}...")
        self._session_message_count = 0
        client.connect(endpoint, 443, keepalive=MQTT_KEEPALIVE_S)
        client.loop_start()
        self._mqtt_client = client

    def _disconnect(self):
        if self._mqtt_client:
            try:
                self._mqtt_client.loop_stop()
                self._mqtt_client.disconnect()
            except Exception:
                pass
            self._mqtt_client = None
        self._connected = False

    def _status(self, msg: str):
        log.info("LiveStream: %s", msg)
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass

    # ── MQTT callbacks ─────────────────────────────

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self._connected = True
            self._status("Connected — subscribing to topics")
            for name, topic in self._topics.items():
                if isinstance(topic, str):
                    client.subscribe(topic, qos=1)  # React app uses QoS 1
                    log.info("  Subscribed: %s → %s", name, topic)
        else:
            self._status(f"Connect rejected: {reason_code}")

    def _on_message(self, client, userdata, msg):
        self._message_count += 1
        self._session_message_count += 1
        self._last_message_time = time.time()

        decoded = self._decode_protobuf(msg.payload)
        if decoded:
            self._last_decoded = decoded
            if self._on_data:
                try:
                    self._on_data(decoded)
                except Exception as e:
                    log.debug("on_data callback error: %s", e)
        else:
            # Try JSON (response_stream topic sends JSON)
            try:
                data = json.loads(msg.payload)
                self._last_decoded = data
                if self._on_data:
                    self._on_data(data)
            except Exception:
                log.debug("Unknown message on %s: %d bytes", msg.topic, len(msg.payload))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._connected = False
        if reason_code != 0:
            self._status(f"Disconnected unexpectedly: {reason_code}")

    # ── Protobuf decode ────────────────────────────

    def _decode_protobuf(self, payload: bytes) -> dict | None:
        if not DataMsg_pb2:
            return None
        try:
            msg = DataMsg_pb2.DataMsg()
            msg.ParseFromString(payload)

            # The protobuf timestamp field is a dummy value (always 1000000).
            # Enphase doesn't send a real wall-clock time — the MQTT delivery
            # time IS the timestamp. Use arrival time instead.
            result = {
                "protocol_ver": msg.protocol_ver,
                "timestamp": int(time.time()),
                "timestamp_proto": msg.timestamp,
                "soc": msg.backup_soc,
                "batt_mode": self._enum_name(MeterSummaryData_pb2.BattMode, msg.batt_mode),
                "_fields_present": frozenset(f[0].name for f in msg.ListFields()),
                "_payload_size": len(payload),
            }

            if msg.meters:
                m = msg.meters
                # Active power (milliwatts → watts)
                result["pv_power_w"] = m.pv.agg_p_mw / 1000.0
                result["grid_power_w"] = m.grid.agg_p_mw / 1000.0
                result["load_power_w"] = m.load.agg_p_mw / 1000.0
                result["storage_power_w"] = m.storage.agg_p_mw / 1000.0
                result["generator_power_w"] = m.generator.agg_p_mw / 1000.0
                # Apparent power (millivolt-amps → VA)
                result["pv_apparent_va"] = m.pv.agg_s_mva / 1000.0
                result["grid_apparent_va"] = m.grid.agg_s_mva / 1000.0
                result["load_apparent_va"] = m.load.agg_s_mva / 1000.0
                result["storage_apparent_va"] = m.storage.agg_s_mva / 1000.0
                result["generator_apparent_va"] = m.generator.agg_s_mva / 1000.0
                # Relay states
                result["grid_relay"] = self._enum_name(MeterSummaryData_pb2.MeterSumGridState, m.grid_relay)
                result["gen_relay"] = self._enum_name(MeterSummaryData_pb2.MeterSumGridState, m.gen_relay)
                # Meter-level SOC (may differ from backup_soc)
                result["meter_soc"] = m.soc
                # System topology
                result["phase_count"] = m.phase_count
                result["is_split_phase"] = m.is_split_phase

                # Per-phase active power
                if m.pv.agg_p_ph_mw:
                    result["pv_phase_w"] = [p / 1000.0 for p in m.pv.agg_p_ph_mw]
                if m.grid.agg_p_ph_mw:
                    result["grid_phase_w"] = [p / 1000.0 for p in m.grid.agg_p_ph_mw]
                if m.load.agg_p_ph_mw:
                    result["load_phase_w"] = [p / 1000.0 for p in m.load.agg_p_ph_mw]
                if m.storage.agg_p_ph_mw:
                    result["storage_phase_w"] = [p / 1000.0 for p in m.storage.agg_p_ph_mw]
                if m.generator.agg_p_ph_mw:
                    result["generator_phase_w"] = [p / 1000.0 for p in m.generator.agg_p_ph_mw]

                # Per-phase apparent power
                if m.pv.agg_s_ph_mva:
                    result["pv_phase_va"] = [p / 1000.0 for p in m.pv.agg_s_ph_mva]
                if m.grid.agg_s_ph_mva:
                    result["grid_phase_va"] = [p / 1000.0 for p in m.grid.agg_s_ph_mva]
                if m.load.agg_s_ph_mva:
                    result["load_phase_va"] = [p / 1000.0 for p in m.load.agg_s_ph_mva]
                if m.storage.agg_s_ph_mva:
                    result["storage_phase_va"] = [p / 1000.0 for p in m.storage.agg_s_ph_mva]

                # Grid toggle check
                if m.HasField("grid_toggle_check"):
                    gtc = m.grid_toggle_check
                    result["grid_update_ongoing"] = gtc.update_ongoing
                    result["grid_outage_status"] = gtc.grid_outage_status

            # Power match status (PCU = Power Conditioning Unit = microinverter)
            if msg.HasField("power_match_status"):
                pms = msg.power_match_status
                result["pcu_total"] = pms.totalPCUCount
                result["pcu_running"] = pms.runningPCUCount
                result["power_match"] = pms.status
                result["power_match_supported"] = pms.isSupported

            # Dry contact relay states
            if msg.dry_contact_relay_status:
                result["dry_contacts"] = []
                for dc in msg.dry_contact_relay_status:
                    result["dry_contacts"].append({
                        "id": self._enum_name(MeterSummaryData_pb2.DryContactId, dc.id),
                        "state": self._enum_name(MeterSummaryData_pb2.DryContactRelayState, dc.state),
                    })

            # Dry contact load names
            if msg.dry_contact_relay_name:
                result["dry_contact_names"] = []
                for dcn in msg.dry_contact_relay_name:
                    result["dry_contact_names"].append({
                        "id": self._enum_name(MeterSummaryData_pb2.DryContactId, dcn.id),
                        "load_name": dcn.load_name,
                    })

            # Load controller status
            if msg.load_status:
                result["loads"] = [
                    {"id": ls.id, "relay": ls.relay_status, "power_w": ls.power}
                    for ls in msg.load_status
                ]

            return result
        except Exception as e:
            log.debug("Protobuf decode: %s", e)
            return None

    def _enum_name(self, enum_type, value):
        try:
            return enum_type.Name(value)
        except ValueError:
            return str(value)

    # ── Credential fetching ────────────────────────

    def _get_stream_credentials(self, serial: str) -> dict:
        try:
            s = self.enlighten._session.session
            h = self.enlighten._headers()
            resp = s.get(
                "https://enlighten.enphaseenergy.com/pv/aws_sigv4/livestream.json",
                params={"serial_num": serial}, headers=h, timeout=15,
            )
            if resp.ok:
                return resp.json()
        except Exception as e:
            log.error("Stream credentials: %s", e)
        return {}

    def _get_response_credentials(self, site_id: str) -> dict:
        try:
            s = self.enlighten._session.session
            h = self.enlighten._headers()
            h["username"] = self.enlighten._session.user_id or ""
            resp = s.get(
                f"https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/mqttSignedUrl/{site_id}",
                headers=h, timeout=15,
            )
            if resp.ok:
                return resp.json()
        except Exception as e:
            log.error("Response credentials: %s", e)
        return {}
