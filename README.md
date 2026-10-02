# DC1 Mover

DC1 Mover is a lightweight Portainer stack migration manager for moving standalone Docker Compose stacks between Portainer endpoints/nodes while preserving named volumes and providing pre-flight checks and rollback.

## Features

- Portainer endpoint and stack inventory
- Pre-flight collision checks
- Named-volume migration through the Docker archive API
- Host bind-IP rewrite when moving between nodes
- Target container/health verification with a 120-second timeout
- Automatic source rollback when migration fails
- Manual confirmation or rollback after a successful migration
- SQLite migration history and per-stack migration locking
- Cluster/node capacity overview and migration advisor
- Built-in login and CSRF protection

## Requirements

- Docker Engine with Docker Compose
- Portainer with an API key that can access the relevant endpoints/stacks
- Portainer endpoints named `DC1-NODE...` (the current application filters migration targets by this prefix)

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

Never commit `.env` or a real Portainer API key. `MOVER_SESSION_SECRET` must contain at least 32 characters. The current application connects to Portainer with TLS certificate verification disabled (`verify=False`), so deploy it only in an environment where that is acceptable or change this before exposing it beyond a trusted network.

## Migration behavior

The migration workflow stops the source stack, copies its named volumes, creates the target stack, waits for the target containers/health checks, and leaves the successful source stack stopped for rollback. Confirmation removes the old source stack and its migrated local volumes; rollback removes the target and starts the preserved source again.

## License

MIT
