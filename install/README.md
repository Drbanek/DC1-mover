# DockerStackMover bootstrap v1.13

Bootstrap prepares a clean Ubuntu Server for a DockerStackMover site.

## Address convention

| Role | Host address |
|---|---|
| PORTAINER | .8 (MAIN only) |
| PROXY | .9 |
| MGMT | .10 (MAIN only) |
| NODE | .11-.29, selected during installation |

REMOTE sites normally contain only PROXY and NODE servers. PORTAINER and MGMT stay in MAIN.

## Management overlay

Management is router-agnostic. Every managed PROXY/NODE has its own WireGuard peer and connects outbound to the MAIN WireGuard hub. No public TCP 9001/9100 forwarding is required at REMOTE sites.

Overlay convention:

- network: `10.200.0.0/16`
- MAIN Portainer/hub: `10.200.0.8`
- recommended site mapping: `10.200.<site-number>.<role-octet>`
- PROXY keeps `.9`; NODE keeps `.11-.29` inside the site's overlay range
- Portainer Agent: TCP 9001 over `wg-dsm`
- Node/Capacity Agent: TCP 9100 over `wg-dsm`

Each REMOTE peer uses `PersistentKeepalive = 25`, so it remains usable behind NAT/stateful firewalls without inbound port forwarding. The MAIN hub must be reachable on UDP 51820 (or the configured WireGuard port).

## MAIN hub

Run on the central Portainer host:

```bash
curl -fsSL https://raw.githubusercontent.com/Drbanek/DockerStackMover/main/install/wireguard-hub.sh -o /tmp/dsm-wg-hub.sh
sudo bash /tmp/dsm-wg-hub.sh init
```

The command prints the MAIN public key. For every enrolled server:

```bash
sudo bash /tmp/dsm-wg-hub.sh add-peer
```

Enter the server's public key and its unique overlay /32.

## Server bootstrap

```bash
curl -fsSL https://raw.githubusercontent.com/Drbanek/DockerStackMover/main/install/bootstrap.sh -o /tmp/dsm-bootstrap.sh
sudo bash /tmp/dsm-bootstrap.sh
```

The bootstrap detects the active /24 interface, asks for site and role, derives the LAN address, installs Docker, nftables and WireGuard, installs Portainer Agent, prepares Netplan and creates `wg-dsm`.

For NODE it also requires explicit `SMAZAT` confirmation before formatting the DATA disk as XFS and mounting it at `/srv`.

During bootstrap provide:

- MAIN WireGuard endpoint, e.g. `vpn.example.tld:51820`
- MAIN WireGuard public key
- unique management overlay address, e.g. `10.200.2.11/32`
- MAIN Portainer overlay IP, default `10.200.0.8`

The bootstrap prints the server public key. Add it to MAIN with `wireguard-hub.sh add-peer`, then start `wg-quick@wg-dsm`.

## Portainer enrollment

Register environments by overlay IP, never by REMOTE public IP:

- `DC2-PROXY -> 10.200.2.9:9001`
- `DC2-NODE01 -> 10.200.2.11:9001`
- `DC2-NODE02 -> 10.200.2.12:9001`

Node Agent URL follows the same rule, e.g. `http://10.200.2.11:9100`.

## Firewall

Bootstrap nftables accepts TCP 9001/9100 only when it arrives on `wg-dsm` from the configured MAIN management IP and drops those ports from all other interfaces. DockerStackMover can later replace this bootstrap policy using its managed apply/confirm/rollback workflow.

## Test topology

For DC1/DC2 on the same routed network, the first test can use `192.168.52.8:51820` as the WireGuard endpoint. A real REMOTE site should use MAIN's public/DNS endpoint and only requires outbound UDP connectivity.
