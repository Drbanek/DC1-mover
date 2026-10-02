# Portainer Stack Mover – Docker build, deployment and migration guide

This document describes the complete setup used for **Portainer Stack Mover v1.0.0**: building the application, publishing the Docker image to GHCR, deploying it through Portainer, configuring Docker endpoints, and performing a safe stack migration with rollback.

> The goal of v1.0.0 is intentionally narrow: reliably migrate a Docker Compose/Portainer stack and its named volumes between selected Docker endpoints. DNS automation and cross-site traffic switching are planned as a separate follow-up.

## 1. Architecture

A typical installation can contain any number of Docker hosts and locations:

```text
Internet
   |
   +-- Site A / public IP
   |     +-- Reverse proxy
   |     +-- Docker NODE01
   |     +-- Docker NODE02
   |
   +-- Site B / public IP
         +-- Reverse proxy
         +-- Docker NODE01
         +-- Docker NODE02

Management
   +-- Portainer
   +-- Portainer Stack Mover
```

Endpoint names are arbitrary. The application does **not** require names such as `DC1-NODE01`. Migration-capable endpoints are explicitly enabled in the Mover UI.

Docker hosts may be in the same LAN or in another site. Connectivity can be provided by a private routed network/VPN or, where appropriate, tightly firewalled public connectivity. Do not expose Docker management interfaces broadly to the Internet.

## 2. Requirements

- Docker Engine on target hosts
- Portainer with the Docker hosts added as endpoints
- Portainer API token
- GitHub repository containing this project
- GitHub Container Registry (GHCR)
- Network connectivity required by Portainer and the Mover
- Persistent Docker named volumes for application data that must be migrated

The Mover does not require SSH access to individual Docker nodes.

## 3. Repository layout

The application is split into a FastAPI backend and a static web interface:

```text
app/
  main.py
  core.py
  routes/
    general.py
    migration.py
    capacity.py
  static/
    index.html
    base.js
    stacks.js
    migrations.js

Dockerfile
docker-compose.yml
requirements.txt
.env.example
```

## 4. Docker image build

The Docker image is built by GitHub Actions rather than by Portainer.

This is intentional. Building a Compose stack remotely through a Portainer Agent can fail because the build operation has to be transported through the agent connection. The production deployment therefore uses a pre-built image from GHCR.

The resulting image is:

```text
ghcr.io/drbanek/dc1-mover:latest
```

A commit-specific image is also published by CI, allowing a deployment to be tied to a particular source revision.

## 5. GitHub Actions / GHCR flow

The deployment pipeline is:

```text
git push
   |
   v
GitHub Actions
   |
   +-- validate / build
   |
   v
GHCR
ghcr.io/drbanek/dc1-mover
   |
   v
Portainer
   |
   v
Pull and redeploy
```

This keeps compilation/building away from the production Docker nodes and makes deployments reproducible.

Never store Portainer tokens, passwords or session secrets in the repository.

## 6. Portainer deployment

The recommended deployment is a **Git repository stack** in Portainer.

Repository:

```text
https://github.com/Drbanek/DC1-mover
```

Reference:

```text
refs/heads/main
```

Compose path:

```text
docker-compose.yml
```

The Compose deployment uses the already-built GHCR image and `pull_policy: always`.

Required environment variables:

```text
PORTAINER_URL=https://<portainer-address>:9443
PORTAINER_TOKEN=<secret>
MOVER_USER=<username>
MOVER_PASSWORD=<secret>
MOVER_SESSION_SECRET=<secret>
MOVER_BIND_IP=<address-to-listen-on>
MOVER_PORT=8081
```

Keep secrets only in the deployment environment/secret store.

Persistent application state is stored in the Docker volume:

```text
mover-data:/data
```

This includes persistent Mover data such as endpoint settings and migration history.

## 7. Updating the Mover

After a new image has successfully been published to GHCR:

1. Open the Mover stack in Portainer.
2. Use **Pull and redeploy**.
3. Portainer pulls the current `latest` image.
4. The container is recreated.
5. The `mover-data` volume remains attached, so persistent settings/history survive the redeployment.

For a controlled production release, a fixed image tag can be used instead of `latest`.

## 8. Endpoint configuration

Open **Nastavení Docker endpointů** in the Mover.

For every Docker endpoint that may participate in migrations:

- enable **Povolit migrace**;
- optionally specify **Host IP**;
- optionally specify a **Lokalita / skupina**.

Only enabled endpoints are included in the migration pool and stack inventory.

The endpoint name itself has no functional meaning.

### Host IP

For a normal TCP Portainer endpoint, the Mover can derive the host address from a URL such as:

```text
tcp://192.168.52.11:9001
```

If the address cannot be derived automatically (for example with another endpoint type), configure **Host IP** manually.

The Host IP is also used when rewriting host-bound published ports during migration.

Example:

```text
192.168.52.12:8080:8080
        |
        v
10.20.30.11:8080:8080
```

## 9. Migration workflow

A migration follows a deliberately conservative sequence:

1. Read the stack definition and metadata from Portainer.
2. Discover named volumes used by the stack.
3. Run pre-flight checks on the destination.
4. Check for stack, volume and published-port conflicts.
5. Stop the source stack.
6. Copy persistent named-volume data.
7. Create the destination volumes.
8. Restore data to the destination.
9. Rewrite source Host IP bindings to the destination Host IP where necessary.
10. Create/start the stack on the destination endpoint.
11. Poll and verify the destination containers.
12. Leave the original source stopped and preserved for rollback.

The source is **not automatically deleted** after a successful migration.

## 10. Confirming a migration

After verifying that the application works correctly on the destination, use the explicit migration confirmation.

Confirmation removes the preserved source stack/volumes according to the migration workflow and finalizes the move.

Do not confirm until the application and its persistent data have been verified.

## 11. Rollback

If the destination is not satisfactory, use **Rollback**.

Rollback is designed to:

1. remove the migrated destination stack;
2. remove the destination copies created by the migration;
3. restart the original source stack.

This makes the migration process intentionally reversible until it is explicitly confirmed.

## 12. Migration between different sites

The Mover is not limited to Docker nodes on one subnet.

A destination can be in another datacenter/site as long as the required Portainer/Docker communication path works.

Recommended topology:

```text
Site A
  Public IP A
  Proxy A
  Docker nodes A

        <--- private VPN / routed management network --->

Site B
  Public IP B
  Proxy B
  Docker nodes B
```

A VPN/private management network is preferred. If public addressing is used, firewall access should be restricted to the exact required source addresses and services.

### Reverse proxy

For independent sites, each site should normally have its own reverse proxy.

For example:

```text
app.example.com
      |
      +--> Public IP A --> Proxy A --> Docker node A
```

After moving the application to Site B:

```text
app.example.com
      |
      +--> Public IP B --> Proxy B --> Docker node B
```

The v1.0.0 migration engine migrates the Docker workload and data. It does **not** automatically change public DNS between sites.

## 13. DNS and future cross-site switching

Cross-site migration introduces one additional layer: public DNS.

A safe manual procedure is:

1. lower DNS TTL before the planned migration;
2. prepare the destination proxy;
3. migrate the stack;
4. verify the destination;
5. change the application's A/AAAA record to the destination site's public IP;
6. verify DNS, HTTPS and the application;
7. only then confirm the migration.

If rollback is required, restore the original DNS record and roll the stack back.

A future release may implement DNS-provider integration so that DNS switching and DNS rollback can be coordinated with the migration. **Váš Hosting DNS API** is the planned first provider, but this functionality is intentionally outside v1.0.0.

## 14. Security notes

Before exposing the Mover beyond a trusted management network:

- restrict access with a firewall;
- use HTTPS/reverse proxy;
- protect the Portainer API token;
- protect the Mover password and session secret;
- never commit secrets to Git;
- prefer VPN/private routing between sites;
- do not broadly expose Docker or Portainer Agent management ports to the Internet;
- back up important workloads before the first production migration.

The Mover currently communicates with Portainer using the configured API credentials, so compromise of the Mover may imply significant Docker-management access.

## 15. v1.0.0 scope

Version 1.0.0 establishes the tested migration baseline:

- arbitrary Portainer endpoint names;
- explicit selection of migration-enabled endpoints;
- persistent endpoint configuration;
- stack inventory limited to selected endpoints;
- pre-flight checks;
- named-volume transfer;
- destination Host IP rewrite;
- destination verification;
- persistent migration history;
- explicit confirmation;
- tested rollback;
- GHCR-based build/deployment;
- Portainer Git stack deployment.

The migration engine should remain stable while additional integrations are developed separately.

## 16. Planned next step

The next logical extension is DNS integration for migrations between sites.

Initial target:

```text
Portainer Stack Mover
  |
  +-- Migration engine (v1.0.0)
  |
  +-- DNS provider layer
        |
        +-- Váš Hosting
        +-- additional providers later
```

The DNS provider layer should remain optional so that the core Mover continues to work without any external DNS integration.
