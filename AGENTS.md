# AGENTS.md

## Project overview

starling2mqtt is a single-file Python bridge (`starling2mqtt.py`) that polls Nest
devices through a Starling Home Hub's local Developer Connect API
(`http://<hub>:3080/api/connect/v1`) and publishes them to MQTT using Home
Assistant MQTT Discovery. It also accepts commands from Home Assistant and writes
them back to the hub. It runs as a container.

API reference: <https://sidewinder.starlinghome.io/sdc/>. It is pull-only, with no
push or webhooks. Documented limits: at most one property write per second and one
snapshot per camera every 10 seconds.

## Setup

```
pip install -r requirements.txt
```

## Build / Run

```
python starling2mqtt.py
```

Reads `config.yaml` from the working directory (see `config.yaml.example`), with
environment-variable overrides (`ENV_OVERRIDES`). `docker build -t starling2mqtt .`
builds the image. In the container, the working directory is `/data`.

## Test

```
pip install pytest
pytest
```

Tests live in `test_starling2mqtt.py` at the repo root. They mock the HTTP session
and the MQTT client, so no hub or broker is needed.

## Repository structure

- `starling2mqtt.py`: the bridge (single module)
- `test_starling2mqtt.py`: unit tests
- `config.yaml.example`: annotated example config
- `docker/README.md`: Docker Hub description (synced on release)
- `Dockerfile`: image build

## Commit and PR conventions

- Commit messages and PR titles must follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`, `ci:`, `build:`, `perf:`, `style:`, `revert:`), optionally with a scope, e.g. `fix(api): handle null response`.
- This repo squash-merges pull requests only; the PR title becomes the final commit message on `main`.
- A "Conventional Commits" CI check enforces this on both PR titles and direct-push commit messages.
- Branch protection on `main`: no force-pushes, no branch deletion, required status checks must pass.
