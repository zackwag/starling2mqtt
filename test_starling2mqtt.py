import json
from unittest.mock import MagicMock, patch

import pytest
import requests

import starling2mqtt
from starling2mqtt import (
    MAX_SNAPSHOT_RETRIES,
    MIN_SNAPSHOT_INTERVAL,
    SNAPSHOT_RETRY_DELAY,
    Bridge,
    StarlingAPI,
    StarlingError,
    apply_env_overrides,
    build_climate_discovery,
    build_device_info,
    build_entity_discovery,
    decode_command,
    device_included,
    device_model,
    encode_value,
    entity_spec,
    humanize,
    property_included,
    require_config,
    state_topic,
    topic_slug,
    validate_config,
)

DOORBELL = {
    "type": "cam",
    "id": "AAAA1111BBBB2222",
    "where": "Entryway",
    "name": "Entryway Doorbell",
    "serialNumber": "AAAA1111BBBB2222",
    "structureName": "Home",
}
PATIO = {**DOORBELL, "id": "CCCC3333DDDD4444", "where": "Patio", "name": "Patio camera"}
THERMOSTAT = {**DOORBELL, "type": "thermostat", "id": "T1", "name": "Hallway"}


def doorbell_props(**overrides):
    props = {
        **DOORBELL,
        "supportsStreaming": True,
        "doorbellPushed": False,
        "motionDetected": False,
        "personDetected": False,
        "quietTime": False,
        "faceDetected:Alice": False,
        "faceDetected:unfamiliar": False,
        "cameraEnabled": True,
        "isOnline": True,
    }
    props.update(overrides)
    return props


def make_config(**sections):
    config = {
        "starling": {"host": "hub", "api_key": "SECRETKEY123"},
        "mqtt": {"broker": "broker"},
    }
    config.update(sections)
    return validate_config(config)


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def bridge():
    api = MagicMock()
    client = MagicMock()
    b = Bridge(make_config(), api, client)
    b.devices = {DOORBELL["id"]: DOORBELL}
    return b


def published(client):
    """{topic: payload} of everything published via the mock client (last write wins)."""
    return {c.args[0]: c.args[1] for c in client.publish.call_args_list}


# --- config ---


class TestConfig:
    def test_require_config_nested(self):
        assert require_config({"a": {"b": 1}}, "a", "b") == 1

    def test_require_config_missing(self):
        with pytest.raises(KeyError, match="a.b"):
            require_config({"a": {}}, "a", "b")

    def test_validate_requires_api_key(self):
        with pytest.raises(KeyError, match="starling.api_key"):
            validate_config({"starling": {"host": "x"}, "mqtt": {"broker": "y"}})

    def test_validate_fills_optional_sections(self):
        config = make_config()
        assert config["devices"] == {} and config["snapshots"] == {}

    def test_env_overrides(self):
        config = apply_env_overrides(
            {"mqtt": {"broker": "a"}},
            {"MQTT_BROKER": "b", "MQTT_PORT": "1884", "STARLING_API_KEY": "k", "UNRELATED": "x"},
        )
        assert config["mqtt"] == {"broker": "b", "port": 1884}
        assert config["starling"] == {"api_key": "k"}


# --- filtering ---


class TestFiltering:
    def test_default_types_exclude_protect(self):
        assert device_included(DOORBELL, {})
        assert not device_included({**DOORBELL, "type": "protect"}, {})

    def test_include_by_name_case_insensitive(self):
        conf = {"include": ["entryway doorbell"]}
        assert device_included(DOORBELL, conf)
        assert not device_included(PATIO, conf)

    def test_exclude_by_id(self):
        assert not device_included(PATIO, {"exclude": [PATIO["id"]]})

    def test_faces_excluded_by_default(self):
        assert not property_included(DOORBELL, "faceDetected:Alice", {})
        assert property_included(DOORBELL, "motionDetected", {})

    def test_include_beats_exclude(self):
        conf = {"include": ["Entryway Doorbell/faceDetected:Alice"]}
        assert property_included(DOORBELL, "faceDetected:Alice", conf)
        assert not property_included(PATIO, "faceDetected:Alice", conf)

    def test_explicit_exclude_replaces_default(self):
        conf = {"exclude": ["Patio camera/animalDetected"]}
        assert property_included(DOORBELL, "faceDetected:Alice", conf)
        assert not property_included(PATIO, "animalDetected", conf)


# --- specs / encoding ---


class TestSpecs:
    def test_known_camera_property(self):
        assert entity_spec("cam", "motionDetected", False)["device_class"] == "motion"

    def test_common_property(self):
        assert entity_spec("cam", "batteryLevel", 20)["device_class"] == "battery"

    def test_prefix_property(self):
        spec = entity_spec("cam", "faceDetected:Alice", False)
        assert spec["platform"] == "binary_sensor" and spec["name"] == "Face Alice"

    def test_unknown_bool_falls_back_to_binary_sensor(self):
        assert entity_spec("cam", "someNewFlag", True) == {
            "platform": "binary_sensor",
            "name": "Some new flag",
        }

    def test_unknown_string_falls_back_to_sensor(self):
        assert entity_spec("cam", "someState", "x")["platform"] == "sensor"

    def test_humanize(self):
        assert humanize("lastLockUnlockMethod") == "Last lock unlock method"

    def test_topic_slug(self):
        assert topic_slug("faceDetected:Jo O'Neil") == "faceDetected_Jo_O_Neil"

    @pytest.mark.parametrize(
        ("value", "expected"), [(True, "true"), (False, "false"), (20, "20"), ("low", "low")]
    )
    def test_encode_value(self, value, expected):
        assert encode_value(value) == expected

    @pytest.mark.parametrize(
        ("payload", "current", "expected"),
        [
            ("ON", True, True),
            ("false", True, False),
            ("21.5", 20.0, 21.5),
            ("40", 35, 40),
            ("locked", "unlocked", "locked"),
            ("true", None, True),
        ],
    )
    def test_decode_command(self, payload, current, expected):
        result = decode_command(payload, current)
        assert result == expected and type(result) is type(expected)

    def test_decode_command_rejects_bad_bool(self):
        with pytest.raises(ValueError):
            decode_command("maybe", False)


# --- device models ---


class TestDeviceModel:
    @pytest.mark.parametrize(
        ("device_type", "props", "expected"),
        [
            ("cam", {"motionDetected": False}, "Nest Cam"),
            ("cam", {"doorbellPushed": False}, "Nest Doorbell"),
            ("cam", {"cameraModel": "Nest Cam IQ Outdoor"}, "Nest Cam IQ Outdoor"),
            ("thermostat", {}, "Nest Thermostat"),
            ("lock", {}, "Nest × Yale Lock"),
            ("something_new", {}, "Something_new"),
        ],
    )
    def test_device_model(self, device_type, props, expected):
        assert device_model(device_type, props) == expected

    def test_doorbell_detected_even_if_property_excluded(self, bridge):
        # Filters hide entities, but the raw props still identify a doorbell.
        bridge.properties_conf = {"exclude": ["doorbellPushed"]}
        bridge.process_device(DOORBELL, doorbell_props())
        config = json.loads(
            published(bridge.client)[
                "homeassistant/binary_sensor/starling2mqtt_aaaa1111bbbb2222_motiondetected/config"
            ]
        )
        assert config["device"]["model"] == "Nest Doorbell"

    def test_build_device_info_without_props(self):
        assert build_device_info(PATIO, "hub")["model"] == "Nest Cam"


# --- discovery payloads ---


class TestDiscovery:
    def test_switch_has_command_topic(self):
        topic, payload, commands = build_entity_discovery(
            entity_spec("cam", "cameraEnabled", True), DOORBELL, "cameraEnabled",
            {}, [], "s2m", "homeassistant",
        )  # fmt: skip
        cmd = "s2m/AAAA1111BBBB2222/cameraEnabled/set"
        assert topic == "homeassistant/switch/starling2mqtt_aaaa1111bbbb2222_cameraenabled/config"
        assert payload["command_topic"] == cmd
        assert payload["state_topic"] == "s2m/AAAA1111BBBB2222/cameraEnabled"
        assert commands == {cmd: (DOORBELL["id"], "cameraEnabled")}

    def test_binary_sensor_override_payloads(self):
        _, payload, commands = build_entity_discovery(
            entity_spec("cam", "batteryStatus", "low"), PATIO, "batteryStatus",
            {}, [], "s2m", "homeassistant",
        )  # fmt: skip
        assert (payload["payload_on"], payload["payload_off"]) == ("low", "normal")
        assert commands == {}

    def test_lock_commands_target_state(self):
        lock = {**DOORBELL, "type": "lock", "id": "L1"}
        _, payload, commands = build_entity_discovery(
            entity_spec("lock", "currentState", "locked"), lock, "currentState",
            {}, [], "s2m", "homeassistant",
        )  # fmt: skip
        assert payload["command_topic"] == "s2m/L1/targetState/set"
        assert commands == {"s2m/L1/targetState/set": ("L1", "targetState")}
        assert "command_property" not in payload

    def test_no_entity_spec(self):
        assert (
            build_entity_discovery(
                entity_spec("thermostat", "hvacMode", "heat"),
                THERMOSTAT,
                "hvacMode",
                {},
                [],
                "s2m",
                "homeassistant",
            )
            is None
        )

    def test_climate_modes_follow_capabilities(self):
        _, payload, commands = build_climate_discovery(
            THERMOSTAT, {"canHeat": True, "canCool": False}, {}, [], "s2m", "homeassistant"
        )
        assert payload["modes"] == ["off", "heat"]
        assert "s2m/T1/targetTemperature/set" in commands

    def test_climate_heat_cool(self):
        _, payload, _ = build_climate_discovery(
            THERMOSTAT, {"canHeat": True, "canCool": True}, {}, [], "s2m", "homeassistant"
        )
        assert payload["modes"] == ["off", "heat", "cool", "heat_cool"]
        assert '"heatCool": "heat_cool"' in payload["mode_state_template"]


# --- API client ---


def fake_response(status=200, body=None, content=b"", content_type="application/json"):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"Content-Type": content_type}
    resp.content = content
    if body is None:
        resp.json.side_effect = ValueError
    else:
        resp.json.return_value = body
    return resp


class TestStarlingAPI:
    def make(self, response=None, error=None):
        api = StarlingAPI("hub", 3080, "SECRETKEY123")
        api.session = MagicMock()
        if error:
            api.session.request.side_effect = error
        else:
            api.session.request.return_value = response
        return api

    def test_device_properties(self):
        api = self.make(fake_response(body={"status": "OK", "properties": {"a": 1}}))
        assert api.device("X") == {"a": 1}
        args, kwargs = api.session.request.call_args
        assert args == ("GET", "http://hub:3080/api/connect/v1/devices/X")
        assert kwargs["params"] == {"key": "SECRETKEY123"}

    def test_error_body_raises_with_code(self):
        body = {"status": "Error", "code": "DEVICE_NOT_FOUND", "message": "nope"}
        api = self.make(fake_response(404, body))
        with pytest.raises(StarlingError) as exc:
            api.device("X")
        assert exc.value.code == "DEVICE_NOT_FOUND"

    def test_connection_error_redacts_key(self):
        api = self.make(error=requests.ConnectionError("failed: /status?key=SECRETKEY123"))
        with pytest.raises(StarlingError) as exc:
            api.status()
        assert "SECRETKEY123" not in str(exc.value)
        assert exc.value.code == "CONNECTION_ERROR"

    def test_set_properties_returns_set_status(self):
        body = {"status": "OK", "setStatus": {"quietTime": "OK"}}
        api = self.make(fake_response(body=body))
        assert api.set_properties("X", {"quietTime": True}) == {"quietTime": "OK"}
        assert api.session.request.call_args.kwargs["json"] == {"quietTime": True}

    def test_snapshot_returns_bytes(self):
        api = self.make(fake_response(content=b"\xff\xd8", content_type="image/jpeg"))
        assert api.snapshot("X") == b"\xff\xd8"

    def test_snapshot_error(self):
        body = {"status": "Error", "code": "NO_SNAPSHOT_CAMERA_OFF", "message": "off"}
        api = self.make(fake_response(400, body))
        with pytest.raises(StarlingError) as exc:
            api.snapshot("X")
        assert exc.value.code == "NO_SNAPSHOT_CAMERA_OFF"


# --- bridge ---


class TestProcessDevice:
    def test_publishes_discovery_and_state(self, bridge):
        bridge.process_device(DOORBELL, doorbell_props())
        out = published(bridge.client)
        assert out["starling2mqtt/AAAA1111BBBB2222/motionDetected"] == "false"
        motion = json.loads(
            out["homeassistant/binary_sensor/starling2mqtt_aaaa1111bbbb2222_motiondetected/config"]
        )
        assert motion["device"]["name"] == "Entryway Doorbell"
        assert motion["device"]["suggested_area"] == "Entryway"
        assert motion["availability_mode"] == "all"

    def test_metadata_and_excluded_properties_are_skipped(self, bridge):
        bridge.process_device(DOORBELL, doorbell_props())
        topics = " ".join(published(bridge.client))
        assert "faceDetected" not in topics
        assert "supportsStreaming" not in topics
        assert "/serialNumber" not in topics

    def test_quiet_time_is_a_switch(self, bridge):
        bridge.process_device(DOORBELL, doorbell_props())
        out = published(bridge.client)
        assert "homeassistant/switch/starling2mqtt_aaaa1111bbbb2222_quiettime/config" in out
        assert not any("quiet_time" in t for t in out)

    def test_snapshot_camera_published_for_cams(self, bridge):
        bridge.process_device(DOORBELL, doorbell_props())
        assert "homeassistant/camera/starling2mqtt_aaaa1111bbbb2222_snapshot/config" in published(
            bridge.client
        )

    def test_unchanged_state_not_republished(self, bridge):
        bridge.process_device(DOORBELL, doorbell_props())
        count = bridge.client.publish.call_count
        bridge.process_device(DOORBELL, doorbell_props())
        assert bridge.client.publish.call_count == count

    def test_changed_state_republished(self, bridge):
        bridge.process_device(DOORBELL, doorbell_props())
        bridge.client.publish.reset_mock()
        bridge.process_device(DOORBELL, doorbell_props(motionDetected=True))
        assert published(bridge.client) == {"starling2mqtt/AAAA1111BBBB2222/motionDetected": "true"}

    def test_rising_edge_requests_snapshot(self, bridge):
        bridge.process_device(DOORBELL, doorbell_props())
        assert bridge.snapshot_pending == set()
        bridge.process_device(DOORBELL, doorbell_props(doorbellPushed=True))
        assert bridge.snapshot_pending == {DOORBELL["id"]}

    def test_battery_entities_for_patio(self, bridge):
        props = {**PATIO, "batteryLevel": 20, "batteryStatus": "low", "batteryIsCharging": True}
        bridge.process_device(PATIO, props)
        out = published(bridge.client)
        assert out["starling2mqtt/CCCC3333DDDD4444/batteryLevel"] == "20"
        battery = json.loads(
            out["homeassistant/sensor/starling2mqtt_cccc3333dddd4444_batterylevel/config"]
        )
        assert battery["device_class"] == "battery"
        assert battery["unit_of_measurement"] == "%"


class TestCommands:
    def test_switch_command_queues_typed_write(self, bridge):
        bridge.process_device(DOORBELL, doorbell_props())
        msg = MagicMock(topic="starling2mqtt/AAAA1111BBBB2222/cameraEnabled/set", payload=b"false")
        bridge.on_message(None, None, msg)
        assert bridge.write_queue.get_nowait() == (DOORBELL["id"], {"cameraEnabled": False})

    def test_unknown_topic_ignored(self, bridge):
        msg = MagicMock(topic="starling2mqtt/NOPE/cameraEnabled/set", payload=b"false")
        bridge.on_message(None, None, msg)
        assert bridge.write_queue.empty()

    def test_quiet_time_switch_command(self, bridge):
        bridge.process_device(DOORBELL, doorbell_props())
        msg = MagicMock(topic="starling2mqtt/AAAA1111BBBB2222/quietTime/set", payload=b"true")
        bridge.on_message(None, None, msg)
        assert bridge.write_queue.get_nowait() == (DOORBELL["id"], {"quietTime": True})

    def test_ha_online_requests_republish(self, bridge):
        bridge.on_message(None, None, MagicMock(topic="homeassistant/status", payload=b"online"))
        assert bridge.republish_requested.is_set()

    def test_drain_writes_merges_per_device(self, bridge):
        bridge.write_queue.put(("A", {"y": 2}))
        bridge.write_queue.put(("B", {"z": 3}))
        bridge.write_queue.put(("A", {"x": 9}))
        assert bridge.drain_writes(("A", {"x": 1})) == {"A": {"x": 9, "y": 2}, "B": {"z": 3}}


class TestWrites:
    def test_successful_write_publishes_and_holds(self, bridge):
        bridge.process_device(DOORBELL, doorbell_props())
        bridge.api.set_properties.return_value = {"cameraEnabled": "OK"}
        bridge.perform_write(DOORBELL["id"], {"cameraEnabled": False})
        topic = state_topic("starling2mqtt", DOORBELL["id"], "cameraEnabled")
        assert published(bridge.client)[topic] == "false"
        # Hub cache still says true on the next poll: the held value wins.
        bridge.client.publish.reset_mock()
        bridge.process_device(DOORBELL, doorbell_props(cameraEnabled=True))
        assert topic not in published(bridge.client)

    def test_hold_expires(self, bridge):
        bridge.process_device(DOORBELL, doorbell_props())
        bridge.api.set_properties.return_value = {"cameraEnabled": "OK"}
        with patch.object(starling2mqtt.time, "monotonic", return_value=1000.0):
            bridge.perform_write(DOORBELL["id"], {"cameraEnabled": False})
        with patch.object(starling2mqtt.time, "monotonic", return_value=1100.0):
            bridge.process_device(DOORBELL, doorbell_props(cameraEnabled=True))
        topic = state_topic("starling2mqtt", DOORBELL["id"], "cameraEnabled")
        assert published(bridge.client)[topic] == "true"

    def test_rejected_write_not_published(self, bridge):
        bridge.api.set_properties.return_value = {"cameraEnabled": "READ_ONLY"}
        bridge.perform_write(DOORBELL["id"], {"cameraEnabled": False})
        bridge.client.publish.assert_not_called()

    def test_write_error_logged_not_raised(self, bridge):
        bridge.api.set_properties.side_effect = StarlingError("boom", "X")
        bridge.perform_write(DOORBELL["id"], {"cameraEnabled": False})
        bridge.client.publish.assert_not_called()


class TestSnapshots:
    def test_rate_limited_per_camera(self, bridge):
        bridge.request_snapshot("A")
        assert bridge.due_snapshots(100.0) == ["A"]
        bridge.request_snapshot("A")
        assert bridge.due_snapshots(100.0 + MIN_SNAPSHOT_INTERVAL - 1) == []
        assert bridge.due_snapshots(100.0 + MIN_SNAPSHOT_INTERVAL) == ["A"]

    def test_take_snapshot_publishes_bytes(self, bridge):
        bridge.api.snapshot.return_value = b"\xff\xd8jpeg"
        bridge.take_snapshot(DOORBELL["id"])
        bridge.client.publish.assert_called_once_with(
            "starling2mqtt/AAAA1111BBBB2222/snapshot", b"\xff\xd8jpeg", qos=1, retain=True
        )

    def test_please_wait_retries_after_delay(self, bridge):
        bridge.api.snapshot.side_effect = StarlingError("wait", "NO_SNAPSHOT_PLEASE_WAIT")
        with patch.object(starling2mqtt.time, "monotonic", return_value=100.0):
            assert bridge.due_snapshots(100.0) == []
            bridge.take_snapshot(DOORBELL["id"])
        assert bridge.snapshot_pending == {DOORBELL["id"]}
        assert bridge.due_snapshots(100.0 + SNAPSHOT_RETRY_DELAY - 1) == []
        assert bridge.due_snapshots(100.0 + SNAPSHOT_RETRY_DELAY) == [DOORBELL["id"]]
        bridge.client.publish.assert_not_called()

    def test_please_wait_gives_up_after_max_retries(self, bridge, caplog):
        bridge.api.snapshot.side_effect = StarlingError("wait", "NO_SNAPSHOT_PLEASE_WAIT")
        for _ in range(MAX_SNAPSHOT_RETRIES):
            bridge.take_snapshot(DOORBELL["id"])
            bridge.snapshot_pending.clear()
        assert "failed" not in caplog.text
        bridge.take_snapshot(DOORBELL["id"])
        assert bridge.snapshot_pending == set()
        assert "Snapshot for AAAA1111BBBB2222 failed" in caplog.text

    def test_success_resets_retry_count(self, bridge):
        bridge.api.snapshot.side_effect = [StarlingError("wait", "NO_SNAPSHOT_PLEASE_WAIT"), b"jpg"]
        bridge.take_snapshot(DOORBELL["id"])
        bridge.take_snapshot(DOORBELL["id"])
        assert bridge.snapshot_retries == {}

    def test_other_errors_not_retried(self, bridge):
        bridge.api.snapshot.side_effect = StarlingError("off", "NO_SNAPSHOT_CAMERA_OFF")
        bridge.take_snapshot(DOORBELL["id"])
        assert bridge.snapshot_pending == set()

    def test_skips_disabled_camera(self, bridge):
        bridge.state[DOORBELL["id"]] = {"cameraEnabled": False}
        bridge.take_snapshot(DOORBELL["id"])
        bridge.api.snapshot.assert_not_called()

    def test_periodic_queues_cameras(self, bridge):
        bridge.queue_periodic_snapshots()
        assert bridge.snapshot_pending == {DOORBELL["id"]}

    def test_disabled_snapshots(self):
        b = Bridge(make_config(snapshots={"enabled": False}), MagicMock(), MagicMock())
        b.request_snapshot("A")
        assert b.snapshot_pending == set()


class TestHubAndCleanup:
    def test_refresh_filters_devices_and_publishes_hub(self, bridge):
        bridge.devices = {}
        bridge.api.status.return_value = {
            "apiVersion": 3.3,
            "apiReady": True,
            "connectedToNest": True,
            "permissions": {"read": True, "write": True, "camera": True},
        }
        bridge.api.devices.return_value = [DOORBELL, {**DOORBELL, "type": "protect", "id": "P"}]
        assert bridge.refresh_devices()
        assert list(bridge.devices) == [DOORBELL["id"]]
        out = published(bridge.client)
        assert out["starling2mqtt/hub/availability"] == "online"
        assert out["starling2mqtt/hub/connectedToNest"] == "true"

    def test_refresh_failure_marks_hub_offline(self, bridge):
        bridge.api.status.side_effect = StarlingError("down", "CONNECTION_ERROR")
        assert not bridge.refresh_devices()
        out = published(bridge.client)
        assert out["starling2mqtt/hub/availability"] == "offline"
        assert out["starling2mqtt/hub/reachable"] == "false"

    def test_missing_camera_permission_disables_snapshots(self, bridge):
        bridge.api.status.return_value = {"apiReady": True, "permissions": {"write": True}}
        bridge.api.devices.return_value = []
        bridge.refresh_devices()
        assert not bridge.snapshots_enabled

    def test_stale_discovery_cleared(self, bridge):
        bridge.previously_published = {"homeassistant/sensor/old/config"}
        bridge.process_device(DOORBELL, doorbell_props())
        bridge.client.publish.reset_mock()
        bridge.cleanup_stale_discovery()
        bridge.client.publish.assert_called_once_with(
            "homeassistant/sensor/old/config", "", qos=1, retain=True
        )
        with open(starling2mqtt.PUBLISHED_FILE) as f:
            saved = json.load(f)
        assert "homeassistant/sensor/old/config" not in saved
        assert saved == sorted(bridge.discovery)

    def test_poll_error_logged_once(self, bridge, caplog):
        bridge.api.device.side_effect = StarlingError("gone", "DEVICE_NOT_FOUND")
        assert not bridge.poll_devices()
        assert not bridge.poll_devices()
        assert caplog.text.count("Polling 'Entryway Doorbell' failed") == 1
