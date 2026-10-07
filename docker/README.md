# starling2mqtt

A Starling Home Hub → MQTT bridge for Home Assistant. It polls Nest cameras, doorbells,
thermostats, locks and more through the hub's local Developer Connect API and creates
them in Home Assistant through MQTT Discovery. That includes switches for camera on/off
and quiet time, and still snapshots as camera entities.

Source & full docs: <https://github.com/zackwag/starling2mqtt>

## Tags

| Tag | Meaning |
| --- | --- |
| `latest` | Newest stable release |
| `X.Y.Z` | Exact release |
| `X.Y` | Latest patch of that minor line |

Images are multi-arch: `linux/amd64` and `linux/arm64`.

## Quick start

1. Enable **Developer Connect** in the Starling app and create an API key. Give it
   `read`, plus `write` to control devices and `camera` for snapshots.
2. Create `config.yaml` from
   [`config.yaml.example`](https://github.com/zackwag/starling2mqtt/blob/main/config.yaml.example).
   Fill in `starling.host`, `starling.api_key` and the `mqtt` block.

### docker run

```bash
docker run -d --name starling2mqtt --restart unless-stopped --init \
  -v "$PWD/config.yaml:/data/config.yaml:ro" \
  -v starling2mqtt-data:/data \
  zackwag/starling2mqtt:latest
```

### docker-compose.yml

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

## Configuration

| Path | Purpose |
| --- | --- |
| `/data/config.yaml` | Bridge config. Mount it read-only. |
| `/data` | Holds `published_entities.json`, which is used to remove stale entities from Home Assistant. Use a named volume. |

| Env var | Purpose |
| --- | --- |
| `STARLING_HOST`, `STARLING_PORT`, `STARLING_API_KEY`, `STARLING_POLL_INTERVAL` | Override the `starling` config block |
| `MQTT_BROKER`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_BASE_TOPIC`, `MQTT_DISCOVERY_PREFIX` | Override the `mqtt` config block |
| `LOG_LEVEL` | `INFO` (default) or `DEBUG` |

If every required key is set through environment variables, you don't need a config file.
