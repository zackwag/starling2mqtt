# Contributing to starling2mqtt

starling2mqtt is a lightweight Python bridge that polls Nest devices through a
Starling Home Hub's Developer Connect API and publishes them to MQTT for Home
Assistant discovery. Contributions are welcome, such as bug fixes, mappings for new
device properties, and packaging improvements.

## Getting started

```
git clone https://github.com/zackwag/starling2mqtt.git
cd starling2mqtt
pip install -r requirements.txt
```

To run the bridge end to end you need a Starling Home Hub with Developer Connect
enabled and an MQTT broker. The unit tests need neither.

## Development

Run the bridge locally against `config.yaml` (copy `config.yaml.example` as a
starting point):

```
python starling2mqtt.py
```

Run the tests and the linter:

```
pip install pytest ruff
pytest
ruff format --check . && ruff check .
```

When adding support for a device property, include a sample of the hub's
`GET /devices/{id}` output in the PR if you can (with names and ids redacted).

## Commit messages and pull requests

This repo uses [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `chore:`, etc.). Pull requests are squash-merged, and the **PR title** becomes the commit on `main`, so PR titles must follow this format. The "Conventional Commits" check enforces this automatically.

Direct pushes to `main` are allowed but must also use a Conventional Commits-formatted commit message (validated by the same check).

## Opening a pull request

1. Fork the repo and create a branch off `main`.
2. Make your changes.
3. Open a pull request with a Conventional Commits-formatted title.
4. Wait for CI to pass. Required checks must be green before merge.

## Reporting issues

Use [GitHub Issues](../../issues) for bugs and feature requests.
