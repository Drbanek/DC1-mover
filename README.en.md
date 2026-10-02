# DockerStackMover

[Čeština](README.md) | **English**

DockerStackMover is a lightweight tool for safely migrating standalone Docker Compose stacks between Portainer endpoints. It preserves named volumes, performs pre-flight checks, and provides rollback.

## Features

- Portainer endpoint and stack inventory
- Arbitrary endpoint names; no NODE/DC naming dependency
- Explicit selection of migration-enabled endpoints
- Optional Host IP override for Agent/Edge/non-TCP endpoints
- Cluster and endpoint-capacity overview
- Capacity-based migration advisor
- Pre-flight stack, volume and port collision checks
- Named-volume migration through the Docker Archive API
- Host IP rewrite for published ports
- Target container/health verification with a 120-second timeout
- Automatic source recovery when migration fails
- Manual confirmation or rollback after a successful migration
- Persistent SQLite migration history
- Per-stack migration locking
- Built-in login, session and CSRF protection

## Requirements

- Docker Engine with Docker Compose
- Portainer with an API key that can access the required endpoints/stacks
- Reachable Portainer endpoints
- Migration targets explicitly enabled in the Mover UI

## Installation

```bash
git clone https://github.com/Drbanek/DockerStackMover.git
cd DockerStackMover
cp .env.example .env
```

Edit `.env`, especially `PORTAINER_URL`, `PORTAINER_TOKEN`, `MOVER_PASSWORD`, and `MOVER_SESSION_SECRET`. For the optional Váš Hosting integration, also set `VAS_HOSTING_API_KEY`; never commit the API key.

```bash
docker compose up -d --build
```

The default configuration publishes the UI on `127.0.0.1:8081`. Use `MOVER_BIND_IP` to select the listening address.

For the complete English build, GHCR, Portainer and migration guide, see [docs/DOCKER-BUILD-DEPLOY.en.md](docs/DOCKER-BUILD-DEPLOY.en.md).

## Endpoint configuration

Enable only the Docker endpoints that should participate in migrations. Endpoint names are display-only and can use any naming convention. For ordinary `tcp://host:port` endpoints the Host IP can be derived automatically; otherwise it can be configured manually.

## Migration behavior

The Mover stops the source stack, transfers named volumes, creates the destination stack, verifies the destination containers, and preserves the stopped source for rollback. Only explicit confirmation removes the old source copy.

## Cross-site migration

Endpoints do not have to be on the same LAN. They can be connected over a private/VPN network or appropriately secured public connectivity. Independent sites should normally have their own reverse proxy.

v1.0.0 does not automatically change public DNS. DNS-provider integration for cross-site migrations is planned as a future extension.

## Security

Never commit `.env`, Portainer API tokens, passwords, or session secrets. `MOVER_SESSION_SECRET` must contain at least 32 characters. The current application connects to Portainer with TLS verification disabled (`verify=False`), so keep the Mover on a trusted management network until TLS handling is changed.

## v1.0.0

v1.0.0 is the tested migration-engine baseline: stack and persistent-volume migration, Host IP rewrite, pre-flight checks, destination verification, persistent history, confirmation and rollback.

## License

MIT License — Copyright (c) 2026 Lukáš Kačírek.
