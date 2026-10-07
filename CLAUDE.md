# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md

## Commands

```
pytest                                                          # full suite
pytest test_starling2mqtt.py::TestWrites                        # one class
pytest test_starling2mqtt.py::TestWrites::test_hold_expires     # one test
ruff format --check . && ruff check .                           # what the Lint CI job runs
ruff format .                                                   # auto-format (line-length 100)
```

Tests build a `Bridge` around a `MagicMock` API and MQTT client and call its methods
directly. Keep new logic in small methods or pure functions rather than inline in `run()`.

## Architecture

Everything lives in `starling2mqtt.py`. `main()` loads the config, builds `StarlingAPI`
and the paho client (with the LWT on `<base>/bridge/status`), wires the `Bridge`
callbacks, calls `connect_async()` + `loop_start()`, then blocks in `Bridge.run()`.

**Threads.** `run()` is the poll loop on the main thread. `write_worker` (writes to the
hub) and `snapshot_worker` are daemon threads. `on_message` runs on paho's network
thread. Shared dicts on `Bridge` are guarded by `self.lock`.

**Poll loop.** Every `device_refresh_interval` it runs `refresh_devices()`, which reads
`/status` and `/devices`, publishes the hub entities, and filters devices with
`device_included()`. Every `poll_interval` it runs `poll_devices()`, which calls `GET
/devices/{id}` per device and then `process_device()`. `poll_now` (an Event) wakes the
loop early after a write or when HA comes online.

**Entity mapping.** `entity_spec(type, prop, value)` looks up specs in this order:
`TYPE_SPECS[type]`, then `COMMON_SPECS`, then `PREFIX_SPECS` (for `faceDetected:<name>`
and similar), then a fallback based on the value's type. A spec is the HA platform, a
name, and extra keys passed straight into the discovery payload. `NO_ENTITY` publishes
state without an entity; the composite climate and lock entities read those topics.
`command_property` lets an entity send commands to a different property (a lock reads
`currentState` and writes `targetState`). Metadata fields (`METADATA_PROPERTIES`) never
become entities. Thermostats also get a composite `climate` entity from
`build_climate_discovery()`. Cameras get a snapshot `camera` entity and a "Refresh
snapshot" button when snapshots are enabled.

**Topics.** State goes to `<base>/<device_id>/<topic_slug(prop)>` and commands to the
same topic plus `/set`. Buttons use `<base>/<device_id>/button/<key>`. `on_message`
subscribes with wildcards and dispatches through the `self.commands` and `self.buttons`
lookups, which are filled in as discovery is published. `decode_command()` converts the
payload to the type of the property's current value.

**Writes.** Commands are queued. `write_worker` merges everything queued into one POST
per device (`drain_writes`) and keeps at least `MIN_SET_INTERVAL` between POSTs. On
success it publishes the new value and records a `write_holds` entry, so for
`WRITE_HOLD_SECONDS` a stale poll can't flip the state back (`held_value()`).

**Snapshots.** `request_snapshot()` adds a camera to `snapshot_pending`. The snapshot
worker takes the ones that `due_snapshots()` says are past `MIN_SNAPSHOT_INTERVAL`.
Triggers: a configured event property changing to true, the periodic interval, and the
refresh button. JPEG bytes are published retained. The undocumented
`NO_SNAPSHOT_PLEASE_WAIT` error re-queues the camera after `SNAPSHOT_RETRY_DELAY`
(`schedule_snapshot_retry()`), up to `MAX_SNAPSHOT_RETRIES` times in a row.

**Availability.** Device entities use both `<base>/bridge/status` (the LWT) and
`<base>/hub/availability`, with `availability_mode: all`. Hub entities use only the
bridge status.

**Discovery bookkeeping.** Publish discovery through `publish_discovery()` and state
through `publish_state()`. Both skip unchanged payloads and cache what they sent so
`republish_all()` can resend everything when MQTT reconnects or `homeassistant/status`
reports `online`. Every discovery topic ever published is saved in
`published_entities.json`. After the first fully successful refresh and poll,
`cleanup_stale_discovery()` clears the retained configs that this run didn't publish.

## Conventions

- Broad `except Exception` is used on purpose where the daemon must survive (the poll
  loop, worker threads, `on_message`, persistence), always with a
  `# noqa: BLE001 - <reason>` comment. Follow that pattern.
- `StarlingAPI` wraps every `requests` error in `StarlingError` and removes the API key
  from the message (`_redact`), because urllib3 errors include the full URL. Never log
  raw request exceptions.
- New config keys: add a `DEFAULT_*` constant, read it in `Bridge.__init__` (or `main()`),
  add any env override to `ENV_OVERRIDES`, and document it in the README tables and
  `config.yaml.example`.
- New device properties: add a spec to `TYPE_SPECS` or `COMMON_SPECS` rather than
  special-casing them in `process_device()`.
- Releases are driven by release-please. It bumps `__version__` in `starling2mqtt.py`
  (the `x-release-please-version` marker), `pyproject.toml` and `CHANGELOG.md`, so don't
  edit those by hand. Merging the release PR pushes a `v*` tag, which triggers the
  multi-arch Docker Hub publish.
