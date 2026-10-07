import fnmatch
import json
import logging
import os
import queue
import re
import signal
import sys
import threading
import time

import paho.mqtt.client as mqtt
import requests
import yaml

# App Information
__version__ = "0.1.0"  # x-release-please-version
APP_NAME = "starling2mqtt"
SUPPORT_URL = "https://github.com/zackwag/starling2mqtt"

# Filenames (relative to the working directory: /data in the container)
CONFIG_FILE = os.environ.get("STARLING2MQTT_CONFIG", "config.yaml")
PUBLISHED_FILE = "published_entities.json"

# Default Config Values
DEFAULT_STARLING_PORT = 3080
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_DEVICE_REFRESH_INTERVAL = 300
DEFAULT_REQUEST_TIMEOUT = 10
DEFAULT_MQTT_PORT = 1883
DEFAULT_CLIENT_ID = "starling2mqtt"
DEFAULT_BASE_TOPIC = "starling2mqtt"
DEFAULT_DISCOVERY_PREFIX = "homeassistant"
DEFAULT_DEVICE_TYPES = ["cam", "thermostat", "temp_sensor", "lock", "home_away_control", "weather"]
DEFAULT_EXCLUDE_PROPERTIES = ["faceDetected:*", "supportsStreaming"]
DEFAULT_SNAPSHOTS_ENABLED = True
DEFAULT_SNAPSHOT_INTERVAL = 300
DEFAULT_SNAPSHOT_EVENTS = ["motionDetected", "personDetected", "doorbellPushed"]

# Limits from the Starling Developer Connect docs.
MIN_SET_INTERVAL = 1.0
MIN_SNAPSHOT_INTERVAL = 10.0

MAX_QUEUED_MESSAGES = 1000

# After a successful write, ignore a contradicting polled value for this long:
# the hub's cache can lag the write by a poll or two, which would otherwise make
# a toggled switch flip back and forth in Home Assistant.
WRITE_HOLD_SECONDS = 5.0

# Environment variables that override config.yaml: name -> (section, key, cast).
ENV_OVERRIDES = {
    "STARLING_HOST": ("starling", "host", str),
    "STARLING_PORT": ("starling", "port", int),
    "STARLING_API_KEY": ("starling", "api_key", str),
    "STARLING_POLL_INTERVAL": ("starling", "poll_interval", float),
    "MQTT_BROKER": ("mqtt", "broker", str),
    "MQTT_PORT": ("mqtt", "port", int),
    "MQTT_USERNAME": ("mqtt", "username", str),
    "MQTT_PASSWORD": ("mqtt", "password", str),
    "MQTT_BASE_TOPIC": ("mqtt", "base_topic", str),
    "MQTT_DISCOVERY_PREFIX": ("mqtt", "discovery_prefix", str),
}

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def log_info(msg):
    log.info(f"{APP_NAME}: {msg}")


def log_warning(msg):
    log.warning(f"{APP_NAME}: {msg}")


def log_error(msg):
    log.error(f"{APP_NAME}: {msg}")


def log_debug(msg):
    log.debug(f"{APP_NAME}: {msg}")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def apply_env_overrides(config, environ=None):
    """Overlay ENV_OVERRIDES onto config (mutates and returns it)."""
    environ = os.environ if environ is None else environ
    for name, (section, key, cast) in ENV_OVERRIDES.items():
        if environ.get(name):
            config.setdefault(section, {})[key] = cast(environ[name])
    return config


def require_config(cfg, *keys):
    """Walk nested keys, raising KeyError naming the missing path."""
    value = cfg
    path = []
    for key in keys:
        path.append(key)
        if not isinstance(value, dict) or value.get(key) in (None, ""):
            raise KeyError(f"Missing required config key: {'.'.join(path)}")
        value = value[key]
    return value


def validate_config(config):
    if not isinstance(config, dict):
        raise TypeError("Config must be a mapping")
    require_config(config, "starling", "host")
    require_config(config, "starling", "api_key")
    require_config(config, "mqtt", "broker")
    for section in ("devices", "properties", "snapshots"):
        if config.get(section) is None:
            config[section] = {}
    return config


def load_config():
    """Load config.yaml + env overrides, exiting the process on failure."""
    try:
        with open(CONFIG_FILE, "r") as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        log_warning(f"Config file '{CONFIG_FILE}' not found; using environment only.")
        config = {}
    except yaml.YAMLError as e:
        log_error(f"Failed to parse '{CONFIG_FILE}': {e}")
        sys.exit(1)
    try:
        return validate_config(apply_env_overrides(config))
    except (KeyError, TypeError) as e:
        log_error(f"Invalid config: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Starling Developer Connect API
# ---------------------------------------------------------------------------


class StarlingError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class StarlingAPI:
    def __init__(self, host, port, api_key, timeout=DEFAULT_REQUEST_TIMEOUT):
        self.base_url = f"http://{host}:{port}/api/connect/v1"
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()

    def _redact(self, text):
        # requests/urllib3 exceptions embed the full URL, including ?key=...
        return str(text).replace(self.api_key, "***")

    def _request(self, method, path, **kwargs):
        try:
            return self.session.request(
                method,
                f"{self.base_url}{path}",
                params={"key": self.api_key},
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as e:
            raise StarlingError(self._redact(e), "CONNECTION_ERROR") from None

    def _json(self, method, path, **kwargs):
        resp = self._request(method, path, **kwargs)
        try:
            data = resp.json()
        except ValueError:
            raise StarlingError(f"HTTP {resp.status_code}: non-JSON response", "BAD_RESPONSE")
        if resp.status_code != 200 or data.get("status", "OK") != "OK":
            raise StarlingError(data.get("message", f"HTTP {resp.status_code}"), data.get("code"))
        return data

    def status(self):
        return self._json("GET", "/status")

    def devices(self):
        return self._json("GET", "/devices")["devices"]

    def device(self, device_id):
        return self._json("GET", f"/devices/{device_id}")["properties"]

    def set_properties(self, device_id, properties):
        """POST new values; returns the per-property setStatus dict."""
        return self._json("POST", f"/devices/{device_id}", json=properties).get("setStatus", {})

    def snapshot(self, device_id):
        resp = self._request("GET", f"/devices/{device_id}/snapshot")
        if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("image/"):
            return resp.content
        try:
            data = resp.json()
        except ValueError:
            data = {}
        raise StarlingError(data.get("message", f"HTTP {resp.status_code}"), data.get("code"))


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def device_included(device, devices_conf):
    if device.get("type") not in devices_conf.get("types", DEFAULT_DEVICE_TYPES):
        return False
    keys = {str(device.get("id", "")).lower(), str(device.get("name", "")).lower()}
    include = [str(x).lower() for x in devices_conf.get("include") or []]
    exclude = [str(x).lower() for x in devices_conf.get("exclude") or []]
    if include and not keys & set(include):
        return False
    return not keys & set(exclude)


def property_included(device, prop, properties_conf):
    """Glob patterns match "prop", "<device name>/prop" or "<device id>/prop".

    An include match always wins, so a broad exclude (faceDetected:*) can be
    punched through for specific values (faceDetected:Alice).
    """
    keys = (prop, f"{device.get('name')}/{prop}", f"{device.get('id')}/{prop}")

    def matches(patterns):
        return any(fnmatch.fnmatchcase(k, p) for k in keys for p in patterns)

    if matches(properties_conf.get("include") or []):
        return True
    return not matches(properties_conf.get("exclude", DEFAULT_EXCLUDE_PROPERTIES) or [])


# ---------------------------------------------------------------------------
# Entity specs
# ---------------------------------------------------------------------------

# Device fields that describe the device rather than its state.
METADATA_PROPERTIES = {
    "type",
    "id",
    "where",
    "name",
    "serialNumber",
    "structureName",
    "cameraModel",
    "canHeat",
    "canCool",
}

# A spec with platform None publishes state (e.g. for the climate/lock entity
# to read) but gets no entity of its own. Every other key is passed straight
# through into the discovery payload.
NO_ENTITY = {"platform": None}


def _binary(name, **kw):
    return {"platform": "binary_sensor", "name": name, **kw}


def _sensor(name, **kw):
    return {"platform": "sensor", "name": name, **kw}


def _switch(name, **kw):
    return {"platform": "switch", "name": name, **kw}


TEMPERATURE = {
    "device_class": "temperature",
    "unit_of_measurement": "°C",
    "state_class": "measurement",
}
DIAGNOSTIC = {"entity_category": "diagnostic"}

COMMON_SPECS = {
    "batteryStatus": _binary(
        "Battery status", device_class="battery", payload_on="low", payload_off="normal"
    ),
    "batteryLevel": _sensor(
        "Battery", device_class="battery", unit_of_measurement="%", state_class="measurement"
    ),
    "isOnline": _binary("Online", device_class="connectivity", **DIAGNOSTIC),
    "currentTemperature": _sensor("Temperature", **TEMPERATURE),
    "humidityPercent": _sensor(
        "Humidity", device_class="humidity", unit_of_measurement="%", state_class="measurement"
    ),
}

TYPE_SPECS = {
    "cam": {
        "motionDetected": _binary("Motion", device_class="motion"),
        "personDetected": _binary("Person", device_class="occupancy", icon="mdi:walk"),
        "animalDetected": _binary("Animal", device_class="motion", icon="mdi:paw"),
        "vehicleDetected": _binary("Vehicle", device_class="motion", icon="mdi:car"),
        "soundDetected": _binary("Sound", device_class="sound"),
        "doorbellPushed": _binary("Doorbell", icon="mdi:doorbell"),
        "packageDelivered": _binary("Package delivered", icon="mdi:package-variant-closed"),
        "packageRetrieved": _binary("Package retrieved", icon="mdi:package-variant"),
        "garageDoorState": _binary(
            "Garage door", device_class="garage_door", payload_on="open", payload_off="closed"
        ),
        "batteryIsCharging": _binary("Battery charging", device_class="battery_charging"),
        "runningOnBattery": _binary("Running on battery", icon="mdi:battery", **DIAGNOSTIC),
        "trickleCharging": _binary("Trickle charging", icon="mdi:battery-plus", **DIAGNOSTIC),
        "supportsStreaming": _binary("Supports streaming", icon="mdi:video", **DIAGNOSTIC),
        "cameraEnabled": _switch("Camera", icon="mdi:cctv"),
        "chimeEnabled": _switch("Chime", icon="mdi:bell-ring"),
        "floodlightOn": _switch("Floodlight", icon="mdi:light-flood-down"),
        "quietTime": _switch("Quiet time", icon="mdi:bell-sleep"),
    },
    "thermostat": {
        "hvacMode": NO_ENTITY,
        "targetTemperature": NO_ENTITY,
        "targetHeatingThresholdTemperature": NO_ENTITY,
        "targetCoolingThresholdTemperature": NO_ENTITY,
        "hvacState": _sensor(
            "HVAC state", device_class="enum", options=["off", "heating", "cooling"]
        ),
        "fanRunning": _switch("Fan", icon="mdi:fan"),
        "ecoMode": _switch("Eco mode", icon="mdi:leaf"),
        "hotWaterEnabled": _switch("Hot water", icon="mdi:water-boiler"),
        "humidifierActive": _switch("Humidifier", icon="mdi:air-humidifier"),
        "tempHoldMode": _switch("Temperature hold", icon="mdi:timer-lock"),
        "targetHumidity": {
            "platform": "number",
            "name": "Target humidity",
            "device_class": "humidity",
            "unit_of_measurement": "%",
            "min": 10,
            "max": 60,
            "step": 1,
        },
        "presetSelected": _sensor("Preset", icon="mdi:tune-variant"),
        "sensorSelected": _sensor("Active sensor", icon="mdi:thermometer"),
        "currentHumidifierState": _sensor("Humidifier state", icon="mdi:air-humidifier"),
        "backplateTemperature": _sensor("Backplate temperature", **TEMPERATURE, **DIAGNOSTIC),
        "displayTemperatureUnits": _sensor("Display units", **DIAGNOSTIC),
    },
    "lock": {
        "currentState": {
            "platform": "lock",
            "name": None,
            "command_property": "targetState",
            "payload_lock": "locked",
            "payload_unlock": "unlocked",
            "state_locked": "locked",
            "state_unlocked": "unlocked",
            "state_jammed": "jammed",
        },
        "targetState": NO_ENTITY,
        "autoRelockEnabled": _switch(
            "Auto-relock", icon="mdi:lock-clock", entity_category="config"
        ),
        "oneTouchLockEnabled": _switch(
            "One-touch lock", icon="mdi:gesture-tap", entity_category="config"
        ),
        "privacyModeEnabled": _switch("Privacy mode", icon="mdi:eye-off"),
        "isTampered": _binary("Tamper", device_class="tamper"),
        "lastLockUnlockMethod": _sensor("Last lock method", icon="mdi:key"),
    },
    "home_away_control": {
        "homeState": _switch("Home", icon="mdi:home-account"),
    },
}

# Properties whose name carries a value after a colon (faceDetected:Alice).
PREFIX_SPECS = {
    "faceDetected:": lambda who: _binary(
        f"Face {who}", device_class="occupancy", icon="mdi:face-recognition"
    ),
    "zoneActivityDetected:": lambda zone: _binary(f"Zone {zone}", device_class="motion"),
}


def humanize(prop):
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", prop).replace(":", " ").split()
    return " ".join(words).capitalize()


def entity_spec(device_type, prop, value):
    """Return the entity spec for a property, falling back to one based on value type."""
    spec = TYPE_SPECS.get(device_type, {}).get(prop) or COMMON_SPECS.get(prop)
    if spec:
        return spec
    for prefix, factory in PREFIX_SPECS.items():
        if prop.startswith(prefix):
            return factory(prop[len(prefix) :])
    if isinstance(value, bool):
        return _binary(humanize(prop))
    if isinstance(value, (dict, list)):
        return NO_ENTITY
    return _sensor(humanize(prop))


# ---------------------------------------------------------------------------
# Topics & payloads
# ---------------------------------------------------------------------------


def topic_slug(value):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(value)).strip("_")


def state_topic(base_topic, device_id, prop):
    return f"{base_topic}/{device_id}/{topic_slug(prop)}"


def command_topic(base_topic, device_id, prop):
    return f"{state_topic(base_topic, device_id, prop)}/set"


def button_topic(base_topic, device_id, button):
    return f"{base_topic}/{device_id}/button/{button}"


def unique_id(device_id, key):
    return f"{APP_NAME}_{device_id}_{topic_slug(key)}".lower()


def discovery_topic(prefix, platform, uid):
    return f"{prefix}/{platform}/{uid}/config"


def encode_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def decode_command(payload, current):
    """Parse an MQTT command payload into the type of the property's current value."""
    text = payload.strip()
    if isinstance(current, bool):
        lowered = text.lower()
        if lowered in ("true", "on", "1", "yes"):
            return True
        if lowered in ("false", "off", "0", "no"):
            return False
        raise ValueError(f"not a boolean: {payload!r}")
    if isinstance(current, (int, float)):
        number = float(text)
        return int(number) if isinstance(current, int) and number.is_integer() else number
    if current is None:
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def build_device_info(device, hub_identifier):
    info = {
        "identifiers": [f"{APP_NAME}_{device['id']}".lower()],
        "name": device.get("name") or device["id"],
        "manufacturer": "Google Nest",
        "model": device.get("cameraModel") or humanize(device.get("type", "device")),
        "via_device": hub_identifier,
    }
    if device.get("serialNumber"):
        info["serial_number"] = device["serialNumber"]
    if device.get("where"):
        info["suggested_area"] = device["where"]
    return info


def origin_info():
    return {"name": APP_NAME, "sw_version": __version__, "support_url": SUPPORT_URL}


def build_entity_discovery(spec, device, prop, device_info, availability, base_topic, prefix):
    """Return (discovery_topic, payload, command_mapping) or None for NO_ENTITY specs.

    command_mapping is {command_topic: (device_id, property)} for writable entities.
    """
    platform = spec.get("platform")
    if not platform:
        return None
    device_id = device["id"]
    uid = unique_id(device_id, prop)
    payload = {
        "name": spec["name"],
        "unique_id": uid,
        "device": device_info,
        "origin": origin_info(),
        "availability": availability,
        "availability_mode": "all",
        "state_topic": state_topic(base_topic, device_id, prop),
    }
    commands = {}
    if platform == "binary_sensor":
        payload.update(payload_on="true", payload_off="false")
    elif platform in ("switch", "number", "lock"):
        target = spec.get("command_property", prop)
        cmd = command_topic(base_topic, device_id, target)
        payload["command_topic"] = cmd
        commands[cmd] = (device_id, target)
        if platform == "switch":
            payload.update(
                payload_on="true", payload_off="false", state_on="true", state_off="false"
            )
    for key, value in spec.items():
        if key not in ("platform", "name", "command_property"):
            payload[key] = value
    return discovery_topic(prefix, platform, uid), payload, commands


# Starling hvacMode <-> Home Assistant climate mode.
HVAC_MODE_TO_HA = {"off": "off", "heat": "heat", "cool": "cool", "heatCool": "heat_cool"}


def build_climate_discovery(device, props, device_info, availability, base_topic, prefix):
    device_id = device["id"]
    uid = unique_id(device_id, "climate")
    modes = ["off"]
    if props.get("canHeat", True):
        modes.append("heat")
    if props.get("canCool", True):
        modes.append("cool")
    if "heat" in modes and "cool" in modes:
        modes.append("heat_cool")

    def st(prop):
        return state_topic(base_topic, device_id, prop)

    def ct(prop):
        return command_topic(base_topic, device_id, prop)

    to_ha = json.dumps(HVAC_MODE_TO_HA)
    from_ha = json.dumps({v: k for k, v in HVAC_MODE_TO_HA.items()})
    payload = {
        "name": None,
        "unique_id": uid,
        "device": device_info,
        "origin": origin_info(),
        "availability": availability,
        "availability_mode": "all",
        "modes": modes,
        "mode_state_topic": st("hvacMode"),
        "mode_state_template": f"{{{{ {to_ha}.get(value, 'off') }}}}",
        "mode_command_topic": ct("hvacMode"),
        "mode_command_template": f"{{{{ {from_ha}.get(value, value) }}}}",
        "current_temperature_topic": st("currentTemperature"),
        "temperature_state_topic": st("targetTemperature"),
        "temperature_command_topic": ct("targetTemperature"),
        "temperature_low_state_topic": st("targetHeatingThresholdTemperature"),
        "temperature_low_command_topic": ct("targetHeatingThresholdTemperature"),
        "temperature_high_state_topic": st("targetCoolingThresholdTemperature"),
        "temperature_high_command_topic": ct("targetCoolingThresholdTemperature"),
        "action_topic": st("hvacState"),
        "action_template": "{{ {'heating': 'heating', 'cooling': 'cooling'}.get(value, 'idle') }}",
        "temperature_unit": "C",
        "precision": 0.5,
        "temp_step": 0.5,
    }
    if "humidityPercent" in props:
        payload["current_humidity_topic"] = st("humidityPercent")
    commands = {
        ct(p): (device_id, p)
        for p in (
            "hvacMode",
            "targetTemperature",
            "targetHeatingThresholdTemperature",
            "targetCoolingThresholdTemperature",
        )
    }
    return discovery_topic(prefix, "climate", uid), payload, commands


def build_button_discovery(device, key, name, icon, device_info, availability, base_topic, prefix):
    uid = unique_id(device["id"], key)
    cmd = button_topic(base_topic, device["id"], key)
    payload = {
        "name": name,
        "unique_id": uid,
        "device": device_info,
        "origin": origin_info(),
        "availability": availability,
        "availability_mode": "all",
        "command_topic": cmd,
        "icon": icon,
    }
    return discovery_topic(prefix, "button", uid), payload, cmd


def build_camera_discovery(device, device_info, availability, base_topic, prefix):
    uid = unique_id(device["id"], "snapshot")
    payload = {
        "name": "Snapshot",
        "unique_id": uid,
        "device": device_info,
        "origin": origin_info(),
        "availability": availability,
        "availability_mode": "all",
        "topic": f"{base_topic}/{device['id']}/snapshot",
    }
    return discovery_topic(prefix, "camera", uid), payload


HUB_ENTITIES = {
    # key: (platform, name, extra)
    "reachable": ("binary_sensor", "Reachable", {"device_class": "connectivity"}),
    "connectedToNest": ("binary_sensor", "Nest connected", {"device_class": "connectivity"}),
    "apiReady": ("binary_sensor", "API ready", {"icon": "mdi:api", **DIAGNOSTIC}),
    "apiVersion": ("sensor", "API version", {"icon": "mdi:information-outline", **DIAGNOSTIC}),
}


def build_hub_discovery(hub_device_info, bridge_status_topic, base_topic, prefix):
    """Hub entities are only gated on the bridge being up, so 'Reachable' can show off."""
    configs = []
    for key, (platform, name, extra) in HUB_ENTITIES.items():
        uid = unique_id("hub", key)
        payload = {
            "name": name,
            "unique_id": uid,
            "device": hub_device_info,
            "origin": origin_info(),
            "availability_topic": bridge_status_topic,
            "state_topic": f"{base_topic}/hub/{key}",
            **extra,
        }
        if platform == "binary_sensor":
            payload.update(payload_on="true", payload_off="false")
        configs.append((discovery_topic(prefix, platform, uid), payload))
    return configs


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def load_published():
    try:
        with open(PUBLISHED_FILE) as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()
    except Exception as e:  # noqa: BLE001 - a corrupt cache must not stop the bridge
        log_warning(f"Ignoring unreadable {PUBLISHED_FILE}: {e}")
        return set()


def save_published(topics):
    try:
        with open(PUBLISHED_FILE, "w") as f:
            json.dump(sorted(topics), f, indent=2)
    except Exception as e:  # noqa: BLE001 - persistence is best-effort
        log_warning(f"Could not write {PUBLISHED_FILE}: {e}")


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class Bridge:
    def __init__(self, config, api, client):
        starling = config["starling"]
        mqtt_conf = config["mqtt"]
        snapshots = config["snapshots"]
        self.api = api
        self.client = client
        self.devices_conf = config["devices"]
        self.properties_conf = config["properties"]
        self.poll_interval = float(starling.get("poll_interval", DEFAULT_POLL_INTERVAL))
        self.refresh_interval = float(
            starling.get("device_refresh_interval", DEFAULT_DEVICE_REFRESH_INTERVAL)
        )
        self.base_topic = mqtt_conf.get("base_topic", DEFAULT_BASE_TOPIC)
        self.prefix = mqtt_conf.get("discovery_prefix", DEFAULT_DISCOVERY_PREFIX)
        self.snapshots_enabled = bool(snapshots.get("enabled", DEFAULT_SNAPSHOTS_ENABLED))
        self.snapshot_interval = float(snapshots.get("interval", DEFAULT_SNAPSHOT_INTERVAL))
        self.snapshot_events = set(snapshots.get("events", DEFAULT_SNAPSHOT_EVENTS) or [])

        self.bridge_status_topic = f"{self.base_topic}/bridge/status"
        self.hub_availability_topic = f"{self.base_topic}/hub/availability"
        self.availability = [
            {"topic": self.bridge_status_topic},
            {"topic": self.hub_availability_topic},
        ]
        self.hub_identifier = f"{APP_NAME}_hub"

        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.poll_now = threading.Event()
        self.republish_requested = threading.Event()
        self.write_queue = queue.Queue()

        self.devices = {}  # id -> device summary from /devices
        self.state = {}  # id -> last polled properties
        self.retained = {}  # state topic -> last published payload
        self.discovery = {}  # discovery topic -> payload, this session
        self.commands = {}  # command topic -> (device_id, prop)
        self.buttons = {}  # button topic -> callable
        self.device_errors = {}  # id -> last error code (to log only on change)
        self.write_holds = {}  # (device_id, prop) -> (value, monotonic deadline)
        self.last_snapshot = {}  # id -> monotonic time of last attempt
        self.snapshot_pending = set()
        self.hub_reachable = None
        self.previously_published = load_published()
        self.stale_cleaned = False

    # -- MQTT callbacks ------------------------------------------------------

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log_error(f"MQTT connect failed: {reason_code}")
            return
        log_info("Connected to MQTT broker")
        client.publish(self.bridge_status_topic, "online", qos=1, retain=True)
        client.subscribe(f"{self.prefix}/status")
        client.subscribe(f"{self.base_topic}/+/+/set")
        client.subscribe(f"{self.base_topic}/+/button/+")
        # A broker without persistence loses retained messages on restart.
        self.republish_requested.set()

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            log_warning(f"MQTT disconnected ({reason_code}); paho will reconnect")

    def on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8", errors="replace")
            if msg.topic == f"{self.prefix}/status":
                if payload == "online":
                    log_info("Home Assistant came online; republishing")
                    self.republish_requested.set()
                    self.poll_now.set()
                return
            with self.lock:
                target = self.commands.get(msg.topic)
                button = self.buttons.get(msg.topic)
            if target:
                self.handle_command(*target, payload)
            elif button:
                button()
            else:
                log_debug(f"Ignoring message on unknown topic {msg.topic}")
        except Exception as e:  # noqa: BLE001 - never let a bad message kill paho's thread
            log_error(f"Error handling message on {msg.topic}: {e}")

    # -- Commands -------------------------------------------------------------

    def handle_command(self, device_id, prop, payload):
        with self.lock:
            current = self.state.get(device_id, {}).get(prop)
        try:
            value = decode_command(payload, current)
        except ValueError as e:
            log_warning(f"Bad command for {device_id}/{prop}: {e}")
            return
        self.queue_write(device_id, {prop: value})

    def queue_write(self, device_id, props):
        log_info(f"Queueing write {device_id}: {props}")
        self.write_queue.put((device_id, props))

    def drain_writes(self, first):
        """Merge everything queued right now into one POST body per device."""
        pending = {first[0]: dict(first[1])}
        while True:
            try:
                device_id, props = self.write_queue.get_nowait()
            except queue.Empty:
                return pending
            pending.setdefault(device_id, {}).update(props)

    def write_worker(self):
        last_write = 0.0
        while not self.stop_event.is_set():
            try:
                first = self.write_queue.get(timeout=1)
            except queue.Empty:
                continue
            for device_id, props in self.drain_writes(first).items():
                wait = last_write + MIN_SET_INTERVAL - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                last_write = time.monotonic()
                self.perform_write(device_id, props)
            self.poll_now.set()

    def perform_write(self, device_id, props):
        try:
            results = self.api.set_properties(device_id, props)
        except StarlingError as e:
            log_error(f"Write to {device_id} failed: {e} ({e.code})")
            return
        except Exception as e:  # noqa: BLE001 - keep the writer thread alive
            log_error(f"Write to {device_id} failed: {e}")
            return
        hold_until = time.monotonic() + WRITE_HOLD_SECONDS
        for prop, value in props.items():
            result = results.get(prop, "OK")
            if result != "OK":
                log_error(f"Hub rejected {device_id}/{prop}={value!r}: {result}")
                continue
            log_info(f"Set {device_id}/{prop}={value!r}")
            with self.lock:
                self.write_holds[(device_id, prop)] = (value, hold_until)
                self.state.setdefault(device_id, {})[prop] = value
            self.publish_state(state_topic(self.base_topic, device_id, prop), encode_value(value))

    def held_value(self, device_id, prop, polled):
        """Return the value to publish, preferring a fresh write over a stale poll."""
        with self.lock:
            hold = self.write_holds.get((device_id, prop))
            if not hold:
                return polled
            value, deadline = hold
            if polled == value or time.monotonic() > deadline:
                del self.write_holds[(device_id, prop)]
                return polled
            return value

    # -- Publishing -----------------------------------------------------------

    def publish_discovery(self, topic, payload):
        with self.lock:
            if self.discovery.get(topic) == payload:
                return
            self.discovery[topic] = payload
        self.client.publish(topic, json.dumps(payload), qos=1, retain=True)
        log_debug(f"Published discovery {topic}")
        if topic not in self.previously_published:
            self.previously_published.add(topic)
            save_published(self.previously_published)

    def publish_state(self, topic, payload, force=False):
        with self.lock:
            if not force and self.retained.get(topic) == payload:
                return
            self.retained[topic] = payload
        self.client.publish(topic, payload, qos=1, retain=True)

    def republish_all(self):
        with self.lock:
            discovery = list(self.discovery.items())
            states = list(self.retained.items())
        for topic, payload in discovery:
            self.client.publish(topic, json.dumps(payload), qos=1, retain=True)
        for topic, payload in states:
            self.client.publish(topic, payload, qos=1, retain=True)
        log_info(f"Republished {len(discovery)} discovery configs and {len(states)} states")

    def cleanup_stale_discovery(self):
        """Clear retained configs for entities published by an earlier run but not this one."""
        with self.lock:
            stale = self.previously_published - set(self.discovery)
            self.previously_published = set(self.discovery)
        for topic in sorted(stale):
            self.client.publish(topic, "", qos=1, retain=True)
        if stale:
            log_info(f"Removed {len(stale)} stale entities from Home Assistant")
        save_published(self.previously_published)
        self.stale_cleaned = True

    # -- Hub ------------------------------------------------------------------

    def hub_device_info(self, status):
        return {
            "identifiers": [self.hub_identifier],
            "name": "Starling Home Hub",
            "manufacturer": "Starling Home",
            "model": "Home Hub",
            "sw_version": f"SDC API {status.get('apiVersion')}" if status else None,
        }

    def set_hub_reachable(self, reachable, status=None):
        status = status or {}
        available = reachable and bool(status.get("apiReady"))
        self.publish_state(self.hub_availability_topic, "online" if available else "offline")
        self.publish_state(f"{self.base_topic}/hub/reachable", encode_value(reachable))
        if reachable:
            for key in ("connectedToNest", "apiReady", "apiVersion"):
                self.publish_state(f"{self.base_topic}/hub/{key}", encode_value(status.get(key)))
        if reachable != self.hub_reachable:
            log_info(f"Hub {'reachable' if reachable else 'unreachable'}")
            self.hub_reachable = reachable

    def refresh_devices(self):
        """Refresh hub status and the device list. Returns True on success."""
        try:
            status = self.api.status()
            devices = self.api.devices()
        except StarlingError as e:
            if self.hub_reachable is not False:
                log_error(f"Starling hub unavailable: {e}")
            self.set_hub_reachable(False)
            return False
        self.set_hub_reachable(True, status)
        permissions = status.get("permissions", {})
        if not permissions.get("write"):
            log_warning("API key lacks 'write' permission; commands will fail")
        if self.snapshots_enabled and not permissions.get("camera"):
            log_warning("API key lacks 'camera' permission; disabling snapshots")
            self.snapshots_enabled = False
        for topic, payload in build_hub_discovery(
            self.hub_device_info(status), self.bridge_status_topic, self.base_topic, self.prefix
        ):
            self.publish_discovery(topic, payload)
        included = {d["id"]: d for d in devices if device_included(d, self.devices_conf)}
        with self.lock:
            added = included.keys() - self.devices.keys()
            removed = self.devices.keys() - included.keys()
            self.devices = included
        for device_id in sorted(added):
            d = included[device_id]
            log_info(f"Tracking {d.get('type')} '{d.get('name')}' ({device_id})")
        for device_id in sorted(removed):
            log_info(f"No longer tracking {device_id}")
        return True

    # -- Devices --------------------------------------------------------------

    def process_device(self, device, props):
        device_id = device["id"]
        device_type = device.get("type")
        device = {**device, **{k: props[k] for k in METADATA_PROPERTIES if k in props}}
        device_info = build_device_info(device, self.hub_identifier)
        with self.lock:
            previous = self.state.get(device_id, {})

        for prop, value in props.items():
            if prop in METADATA_PROPERTIES or not property_included(
                device, prop, self.properties_conf
            ):
                continue
            entity = build_entity_discovery(
                entity_spec(device_type, prop, value),
                device,
                prop,
                device_info,
                self.availability,
                self.base_topic,
                self.prefix,
            )
            if entity:
                topic, payload, commands = entity
                with self.lock:
                    self.commands.update(commands)
                self.publish_discovery(topic, payload)
            value = self.held_value(device_id, prop, value)
            props[prop] = value
            self.publish_state(state_topic(self.base_topic, device_id, prop), encode_value(value))
            if (
                prop in self.snapshot_events
                and value is True
                and previous.get(prop) is not True
                and device_type == "cam"
            ):
                log_info(f"{device.get('name')}: {prop}")
                self.request_snapshot(device_id)

        if device_type == "thermostat" and "hvacMode" in props:
            topic, payload, commands = build_climate_discovery(
                device, props, device_info, self.availability, self.base_topic, self.prefix
            )
            with self.lock:
                self.commands.update(commands)
            self.publish_discovery(topic, payload)

        if device_type == "cam" and self.snapshots_enabled:
            topic, payload = build_camera_discovery(
                device, device_info, self.availability, self.base_topic, self.prefix
            )
            self.publish_discovery(topic, payload)
            self.add_button(
                device, "refresh_snapshot", "Refresh snapshot", "mdi:camera-retake",
                device_info, lambda: self.request_snapshot(device_id),
            )  # fmt: skip

        with self.lock:
            self.state[device_id] = props

    def add_button(self, device, key, name, icon, device_info, action):
        topic, payload, cmd = build_button_discovery(
            device, key, name, icon, device_info, self.availability, self.base_topic, self.prefix
        )
        with self.lock:
            self.buttons[cmd] = action
        self.publish_discovery(topic, payload)

    def poll_devices(self):
        """Poll every tracked device. Returns True if all polls succeeded."""
        with self.lock:
            devices = list(self.devices.values())
        ok = True
        for device in devices:
            device_id = device["id"]
            try:
                props = self.api.device(device_id)
            except StarlingError as e:
                ok = False
                if self.device_errors.get(device_id) != e.code:
                    log_error(f"Polling '{device.get('name')}' failed: {e} ({e.code})")
                    self.device_errors[device_id] = e.code
                continue
            if self.device_errors.pop(device_id, None):
                log_info(f"Polling '{device.get('name')}' recovered")
            try:
                self.process_device(device, props)
            except Exception as e:  # noqa: BLE001 - one odd device must not stop the rest
                ok = False
                log_error(f"Processing '{device.get('name')}' failed: {e}")
        return ok

    # -- Snapshots ------------------------------------------------------------

    def request_snapshot(self, device_id):
        if self.snapshots_enabled:
            with self.lock:
                self.snapshot_pending.add(device_id)

    def queue_periodic_snapshots(self):
        if not self.snapshots_enabled or self.snapshot_interval <= 0:
            return
        now = time.monotonic()
        with self.lock:
            for device_id, device in self.devices.items():
                last = self.last_snapshot.get(device_id)
                if device.get("type") == "cam" and (
                    last is None or now - last >= self.snapshot_interval
                ):
                    self.snapshot_pending.add(device_id)

    def due_snapshots(self, now):
        with self.lock:
            due = [
                d
                for d in self.snapshot_pending
                if now - self.last_snapshot.get(d, float("-inf")) >= MIN_SNAPSHOT_INTERVAL
            ]
            self.snapshot_pending.difference_update(due)
            for d in due:
                self.last_snapshot[d] = now
        return due

    def take_snapshot(self, device_id):
        with self.lock:
            enabled = self.state.get(device_id, {}).get("cameraEnabled", True)
        if not enabled:
            log_debug(f"Skipping snapshot for {device_id}: camera is off")
            return
        try:
            image = self.api.snapshot(device_id)
        except StarlingError as e:
            log_warning(f"Snapshot for {device_id} failed: {e} ({e.code})")
            return
        self.client.publish(f"{self.base_topic}/{device_id}/snapshot", image, qos=1, retain=True)
        log_debug(f"Published {len(image)} byte snapshot for {device_id}")

    def snapshot_worker(self):
        while not self.stop_event.is_set():
            for device_id in self.due_snapshots(time.monotonic()):
                try:
                    self.take_snapshot(device_id)
                except Exception as e:  # noqa: BLE001 - keep the snapshot thread alive
                    log_error(f"Snapshot for {device_id} failed: {e}")
            self.stop_event.wait(0.5)

    # -- Main loop ------------------------------------------------------------

    def run(self):
        threading.Thread(target=self.write_worker, name="writer", daemon=True).start()
        threading.Thread(target=self.snapshot_worker, name="snapshots", daemon=True).start()
        next_refresh = 0.0
        while not self.stop_event.is_set():
            try:
                refreshed = True
                if time.monotonic() >= next_refresh:
                    refreshed = self.refresh_devices()
                    next_refresh = time.monotonic() + (
                        self.refresh_interval if refreshed else self.poll_interval * 5
                    )
                if self.hub_reachable:
                    polled = self.poll_devices()
                    if refreshed and polled and not self.stale_cleaned:
                        self.cleanup_stale_discovery()
                    self.queue_periodic_snapshots()
                if self.republish_requested.is_set():
                    self.republish_requested.clear()
                    self.republish_all()
            except Exception as e:  # noqa: BLE001 - the poll loop must survive anything
                log_error(f"Poll loop error: {e}")
            self.poll_now.wait(self.poll_interval)
            self.poll_now.clear()

    def shutdown(self):
        self.stop_event.set()
        self.poll_now.set()


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def create_mqtt_client(mqtt_conf, bridge_status_topic):
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=mqtt_conf.get("client_id", DEFAULT_CLIENT_ID),
    )
    if mqtt_conf.get("username"):
        client.username_pw_set(mqtt_conf["username"], mqtt_conf.get("password"))
    client.will_set(bridge_status_topic, "offline", qos=1, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    # Everything is republished on (re)connect, so there's no need to buffer an
    # unbounded backlog (snapshots included) while the broker is unreachable.
    client.max_queued_messages_set(MAX_QUEUED_MESSAGES)
    return client


def main():
    log_info(f"Starting version {__version__}")
    config = load_config()
    starling = config["starling"]
    mqtt_conf = config["mqtt"]

    api = StarlingAPI(
        starling["host"],
        int(starling.get("port", DEFAULT_STARLING_PORT)),
        str(starling["api_key"]),
        float(starling.get("request_timeout", DEFAULT_REQUEST_TIMEOUT)),
    )
    base_topic = mqtt_conf.get("base_topic", DEFAULT_BASE_TOPIC)
    client = create_mqtt_client(mqtt_conf, f"{base_topic}/bridge/status")
    bridge = Bridge(config, api, client)
    client.on_connect = bridge.on_connect
    client.on_disconnect = bridge.on_disconnect
    client.on_message = bridge.on_message

    def handle_exit(signum, frame):
        log_info("Shutting down")
        bridge.shutdown()

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    client.connect_async(mqtt_conf["broker"], int(mqtt_conf.get("port", DEFAULT_MQTT_PORT)))
    client.loop_start()
    try:
        bridge.run()
    finally:
        try:
            info = client.publish(bridge.bridge_status_topic, "offline", qos=1, retain=True)
            info.wait_for_publish(timeout=5)
        except (RuntimeError, ValueError) as e:
            log_warning(f"Could not publish offline status: {e}")
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    main()
