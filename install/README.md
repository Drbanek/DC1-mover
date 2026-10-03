# DockerStackMover bootstrap v1.12

Bootstrap prepares a clean Ubuntu Server for a DockerStackMover site.

## Address convention

| Role | Host address |
|---|---|
| PORTAINER | .8 (MAIN only) |
| PROXY | .9 |
| MGMT | .10 (MAIN only) |
| NODE | .11-.29, selected during installation |

REMOTE sites normally contain only PROXY and NODE servers. PORTAINER and MGMT stay in MAIN.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/Drbanek/DockerStackMover/main/install/bootstrap.sh -o /tmp/dsm-bootstrap.sh
sudo bash /tmp/dsm-bootstrap.sh
```

The script detects the active /24 interface, current IPv4, gateway and DNS; asks for site/role; derives the target address; checks for an address conflict; updates Ubuntu; installs Docker/Compose and nftables; sets hostname; installs Portainer Agent; and prepares Netplan.

For NODE it also requires an explicit `SMAZAT` confirmation before wiping the selected DATA disk, creates GPT/XFS, mounts it at `/srv` with `prjquota`, and creates `/srv/stacks`.

## REMOTE NAT convention

For a server whose internal last octet is **N**:

- Portainer Agent: public TCP **9000+N** -> server TCP **9001**
- Node Agent: public TCP **9100+N** -> server TCP **9100**

Example DC2-NODE01 at `.11`:

- `PUBLIC_IP:9011 -> NODE01:9001`
- `PUBLIC_IP:9111 -> NODE01:9100`

The bootstrap does not modify the edge router. It prints the required DST-NAT mappings. Restrict those forwards to the trusted MAIN/DC management public IP.

The host bootstrap nftables table protects TCP 9001 so it is accepted only from the trusted management IPv4 supplied during installation. It does not flush Docker's rules. After enrollment, DockerStackMover's Node Agent takes over managed firewall policy with the apply/confirm/automatic-rollback workflow.

## Enroll a REMOTE server

1. Configure the printed edge-router DST-NAT mappings.
2. Add the printed public Portainer Agent address as an Environment in central Portainer.
3. In DockerStackMover Endpoint settings set:
   - Site (for example `DC2`)
   - Role (`PROXY` or `NODE`)
   - Host IP = private server IP
   - Public IP = site's public IP
   - Node Agent URL = printed public Node Agent URL
   - Enable migrations only for NODE.
4. Save settings.
5. Run **Připravit NODE/server**. The preconfigured Agent URL is preserved, so DockerStackMover verifies the new agent through the REMOTE public/NAT path.
6. Configure/confirm the managed firewall.

## Safety

- Existing mounted DATA disks are refused.
- DATA formatting requires typing `SMAZAT`.
- Netplan is backed up before changes and validated with `netplan generate`.
- Target IPv4 is checked by ping and duplicate-address ARP probe.
- Bootstrap firewall uses its own nftables table and never flushes Docker rules.
- DockerStackMover managed firewall changes have a confirmation window and automatic rollback.
