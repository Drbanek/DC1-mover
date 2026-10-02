# Portainer Stack Mover

Portainer Stack Mover is a lightweight Portainer stack migration and cluster manager for moving standalone Docker Compose stacks between Portainer endpoints while preserving named volumes and providing pre-flight checks and rollback.

## Features

- Portainer endpoint and stack inventory
- Configurable migration endpoints independent of endpoint names
- Optional endpoint host-IP override for Agent/Edge/non-TCP setups
- Cluster dashboard and endpoint-capacity overview
- Capacity-based migration advisor
- Pre-flight stack, volume and port collision checks
- Named-volume migration through the Docker archive API
- Host bind-IP rewrite when moving between nodes
- Target container/health verification with a 120-second timeout
- Automatic source rollback when migration fails
- Manual confirmation or rollback after a successful migration
- SQLite migration history and per-stack migration locking
- Built-in login, session and CSRF protection

## Project structure

```text
app/
├── main.py                 # FastAPI entrypoint + static frontend
├── core.py                 # Portainer/Docker helpers, DB, sessions, volume copy
├── routes/
│   ├── general.py          # inventory, detail, targets, pre-flight
│   ├── migration.py        # migration worker + authentication routes
│   └── capacity.py         # cluster, capacity, advisor, confirm/rollback
└── static/
    ├── index.html
    ├── style.css
    ├── base.js
    ├── stacks.js
    └── migrations.js
```

## Requirements

- Docker Engine with Docker Compose
- Portainer with an API key that can access the relevant endpoints/stacks
- Portainer endpoints accessible by the configured API key
- Migration targets explicitly enabled in the UI; endpoint names can be arbitrary

## Installation

```bash
git clone https://github.com/Drbanek/DC1-mover.git
cd DC1-mover
cp .env.example .env
```

Edit `.env`, especially `PORTAINER_URL`, `PORTAINER_TOKEN`, `MOVER_PASSWORD`, and `MOVER_SESSION_SECRET`. Then start the service:

```bash
docker compose up -d --build
```

By default the example configuration publishes the UI on `127.0.0.1:8081`. Set `MOVER_BIND_IP` to the address on which the service should listen.

## Security

Never commit `.env` or a real Portainer API key. `MOVER_SESSION_SECRET` must contain at least 32 characters. The current application connects to Portainer with TLS certificate verification disabled (`verify=False`), so keep the management service on a trusted network unless you change the TLS handling.

## Endpoint configuration

After first login, enable the Portainer endpoints that are allowed to receive migrated stacks. Endpoint names are display-only and may use any naming convention. For ordinary `tcp://host:port` endpoints, the mover derives the host IP automatically. For Portainer Agent, Edge, DNS-based or other endpoint types, set an explicit Host IP in the endpoint settings when host-bound Compose ports need to be rewritten.

Endpoints that are not explicitly enabled are never offered as migration targets. This keeps management or infrastructure Docker environments out of the migration pool without relying on naming conventions.

## Migration behavior

The migration workflow stops the source stack, copies its named volumes, creates the target stack, waits for the target containers/health checks, and leaves the successful source stack stopped for rollback. Confirmation removes the old source stack and its migrated local volumes; rollback removes the target and starts the preserved source again.

## Development

GitHub Actions validates Python syntax, Docker Compose configuration and the Docker image build on pushes and pull requests.

## License

MIT License — Copyright (c) 2026 Lukáš Kačírek.
