# Starling Home Hub to MQTT Bridge

[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)
[![Backend](https://img.shields.io/badge/backend-Python%203-3776AB?style=flat-square&logo=python)](https://www.python.org/)
[![Docker](https://img.shields.io/docker/v/zackwag/starling2mqtt?style=flat-square&logo=docker&label=docker)](https://hub.docker.com/r/zackwag/starling2mqtt)

A lightweight Python bridge that polls Nest devices through the
[Starling Home Hub](https://www.starlinghome.io/)'s local
[Developer Connect API](https://sidewinder.starlinghome.io/sdc/) and publishes
them to MQTT for [Home Assistant](https://www.home-assistant.io/), including
camera snapshots, and lets Home Assistant control them back.

Everything is created with MQTT Discovery, so you don't need a custom integration.

---

## Features

- Camera and doorbell events: motion, person, animal, vehicle, sound, doorbell press,
  package delivered/retrieved, and optionally per-face and per-zone detections
- Switches for camera on/off, doorbell quiet time, chime and floodlight
- Battery level, low-battery and charging sensors for battery cameras
- Still snapshots as an MQTT `camera` entity. They refresh on events, on a timer, and
  from a **Refresh snapshot** button.
- Thermostats as a full `climate` entity (modes, target temperatures, heat/cool range,
  current humidity and HVAC action) plus eco, fan, hold and humidity controls
- Nest × Yale locks, Home/Away, temperature sensors and weather
- A **Starling Home Hub** device with Reachable, Nest connected, API ready and API
  version sensors
- Include and exclude filters by device type, device, and property (glob patterns)
- Only changed values are published, and writes from Home Assistant are coalesced and
  rate-limited to the hub's documented limits
- Entities that disappear (or get filtered out) are removed from Home Assistant
  automatically
- Configured with YAML, with environment-variable overrides for secrets
- Multi-arch Docker image (`linux/amd64`, `linux/arm64`)

---

## How It Works

The Developer Connect API is **pull-only**: it has no webhooks or push channel. The hub serves
reads from a local cache, so polling every couple of seconds is cheap and doesn't touch
Google's rate limits.

```
Starling Hub (HTTP :3080)  ──poll──▶  starling2mqtt  ──retained──▶  MQTT  ──discovery──▶  Home Assistant
                           ◀──POST──                 ◀──/set──────
```

- **State:** `starling2mqtt/<device_id>/<property>` (retained). Booleans are `true`/`false`.
- **Commands:** `starling2mqtt/<device_id>/<property>/set`. The payload is parsed to match
  the property's current type, so `true`/`false`/`ON`/`OFF` work for booleans and plain
  numbers for temperatures.
- **Snapshots:** `starling2mqtt/<device_id>/snapshot`, as raw JPEG bytes (retained).
- **Availability:** `starling2mqtt/bridge/status` is the MQTT Last Will. It goes `offline` if
  the bridge dies. `starling2mqtt/hub/availability` goes `offline` when the hub can't be
  reached or reports it isn't ready. Device entities need **both** to be online, so they
  show as unavailable instead of stale. The hub's own sensors depend only on the bridge,
  so **Reachable** can report `off`.

### Rate limits

The bridge follows the limits in the API docs:

- Property writes: at most one POST per second. Writes queued together are merged into one
  request per device.
- Snapshots: at most one per camera every 10 seconds. Requests that arrive sooner are
  deferred, not dropped.

After a successful write, the bridge publishes the new value right away and ignores a
contradicting poll for 5 seconds. Without that, a toggled switch would flick back while the
hub's cache catches up.

---

## Requirements

- A Starling Home Hub with **Developer Connect** enabled and an API key
  (Starling app → Developer Connect). Give the key `read`. Add `write` to control
  devices from Home Assistant and `camera` for snapshots.
- An MQTT broker (e.g. [Mosquitto](https://mosquitto.org/))
- Home Assistant with the MQTT integration
- Python 3.10+ (or Docker)

---

## Run in Docker

1. Create `config.yaml` from [`config.yaml.example`](config.yaml.example). At minimum, set
   `starling.host`, `starling.api_key` and the `mqtt` block. You can also leave the secrets
   out and pass them as environment variables (see below).
2. Run it:

```yaml
services:
  starling2mqtt:
    image: zackwag/starling2mqtt:latest
    container_name: starling2mqtt
    restart: unless-stopped
    init: true
    environment:
      - STARLING_API_KEY=xxxxxxxxxxxx
      - MQTT_PASSWORD=changeme
    volumes:
      - ./config.yaml:/data/config.yaml:ro
      - starling2mqtt-data:/data

volumes:
  starling2mqtt-data:
```

or

```bash
docker run -d --name starling2mqtt --restart unless-stopped --init \
  -v "$PWD/config.yaml:/data/config.yaml:ro" \
  -v starling2mqtt-data:/data \
  zackwag/starling2mqtt:latest
```

`/data` holds `published_entities.json`, which records the entities the bridge has created.
It's how stale entities get removed after a restart, so keep it on a volume.

To build the image yourself: `docker build -t starling2mqtt .`

---

## Run Locally

```bash
git clone https://github.com/zackwag/starling2mqtt.git
cd starling2mqtt
pip install -r requirements.txt
cp config.yaml.example config.yaml   # then edit it
python starling2mqtt.py
```

Set `LOG_LEVEL=DEBUG` for per-message logging.

---

## Configuration Reference

See [`config.yaml.example`](config.yaml.example) for an annotated example.

### `starling`

| Key | Default | Description |
| --- | --- | --- |
| `host` | *(required)* | Hub IP or hostname |
| `port` | `3080` | HTTP port of the Developer Connect API |
| `api_key` | *(required)* | Developer Connect API key |
| `poll_interval` | `2` | Seconds between device polls |
| `device_refresh_interval` | `300` | Seconds between re-reading the device list and hub status |
| `request_timeout` | `10` | HTTP timeout in seconds |

### `mqtt`

| Key | Default | Description |
| --- | --- | --- |
| `broker` | *(required)* | Broker host |
| `port` | `1883` | Broker port |
| `username` / `password` | *(none)* | Broker credentials |
| `client_id` | `starling2mqtt` | MQTT client id |
| `base_topic` | `starling2mqtt` | Root of every state and command topic |
| `discovery_prefix` | `homeassistant` | Home Assistant discovery prefix |

### `devices`

| Key | Default | Description |
| --- | --- | --- |
| `types` | `[cam, thermostat, temp_sensor, lock, home_away_control, weather]` | Device types to bridge. `protect` also works but is off by default. |
| `include` | `[]` | Only bridge these devices (ids or names, case-insensitive). Empty means all. |
| `exclude` | `[]` | Never bridge these devices |

### `properties`

Patterns are shell-style globs. Each one is matched against `property`,
`<device name>/property` and `<device id>/property`. An **include match always beats an
exclude**, so you can exclude a family and add specific members back.

| Key | Default | Description |
| --- | --- | --- |
| `exclude` | `["faceDetected:*", "supportsStreaming"]` | Properties to skip. Setting this **replaces** the default list. |
| `include` | `[]` | Properties to keep even when an exclude matches |

```yaml
properties:
  exclude: ["faceDetected:*", "supportsStreaming", "Office Camera/animalDetected"]
  include: ["faceDetected:unfamiliar", "Entryway Doorbell/faceDetected:Alice"]
```

### `snapshots`

| Key | Default | Description |
| --- | --- | --- |
| `enabled` | `true` | Publish a snapshot camera entity per camera. Turned off automatically if the key lacks `camera` permission. |
| `interval` | `300` | Periodic refresh in seconds. `0` means snapshots are taken only for events and the button. |
| `events` | `[motionDetected, personDetected, doorbellPushed]` | Properties whose change to `true` triggers a snapshot |

Snapshots are skipped while a camera's **Camera** switch is off.

### Environment overrides

Each of these overrides the matching key in `config.yaml`:

`STARLING_HOST`, `STARLING_PORT`, `STARLING_API_KEY`, `STARLING_POLL_INTERVAL`,
`MQTT_BROKER`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_BASE_TOPIC`,
`MQTT_DISCOVERY_PREFIX`. You can also set `LOG_LEVEL` and `STARLING2MQTT_CONFIG` (the
config file path).

If every required key is set through environment variables, you don't need a
`config.yaml` at all.

---

## Home Assistant

Each Nest device becomes a Home Assistant device with the name and area (`where`) from
Starling, linked to a **Starling Home Hub** device. Entity names follow the device, e.g.:

| Entity | Source property |
| --- | --- |
| `binary_sensor.entryway_doorbell_motion` | `motionDetected` |
| `binary_sensor.entryway_doorbell_person` | `personDetected` |
| `binary_sensor.entryway_doorbell_doorbell` | `doorbellPushed` (true for ~5 s after a press) |
| `switch.entryway_doorbell_quiet_time` | `quietTime` |
| `switch.patio_camera_camera` | `cameraEnabled` |
| `sensor.patio_camera_battery` | `batteryLevel` |
| `binary_sensor.patio_camera_battery_status` | `batteryStatus` (`on` = low) |
| `camera.patio_camera_snapshot` | `/snapshot` |
| `button.patio_camera_refresh_snapshot` | requests a new snapshot |
| `climate.hallway` | thermostat `hvacMode`, `targetTemperature`, … |
| `lock.front_door` | lock `currentState` / `targetState` |

Properties the bridge doesn't know (for example ones added in newer hub firmware) still
come through. Booleans become binary sensors and everything else becomes a sensor. Once you
know what a property is, add a proper mapping to `TYPE_SPECS` in `starling2mqtt.py`.

### Notes

- **Quiet time.** `quietTime` isn't in the published API docs, but the hub accepts writes
  to it (tested on API 3.3). The hub doesn't report when quiet time will end. The switch
  follows the hub, so it turns off on its own once Nest ends quiet time.
- **Temperatures.** The API always reports Celsius. Home Assistant converts to your unit
  system.
- **Video.** Live video uses WebRTC and can't go over MQTT. Use Home Assistant's Nest
  integration or the hub's HomeKit bridge for live streams.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
