# Portainer Stack Mover

**Čeština** | [English](README.en.md)

Portainer Stack Mover je lehký nástroj pro bezpečnou migraci samostatných Docker Compose stacků mezi endpointy v Portaineru. Přenáší named volumes, provádí kontroly před migrací a umožňuje návrat zpět pomocí rollbacku.

## Funkce

- přehled Portainer endpointů a stacků
- libovolné názvy endpointů bez závislosti na pojmenování NODE/DC
- explicitní výběr endpointů povolených pro migrace
- volitelná Host IP pro Agent/Edge/non-TCP endpointy
- přehled clusteru a kapacity endpointů
- doporučení cílového endpointu podle kapacity
- pre-flight kontrola kolizí stacků, volumes a portů
- migrace named volumes přes Docker Archive API
- automatický přepis Host IP u publikovaných portů
- kontrola cílových kontejnerů/health checků s timeoutem 120 sekund
- automatické obnovení zdroje při chybě migrace
- ruční potvrzení nebo rollback po úspěšné migraci
- persistentní historie migrací v SQLite
- zámek proti souběžné migraci stejného stacku
- přihlášení, session a CSRF ochrana

## Struktura projektu

```text
app/
├── main.py
├── core.py
├── routes/
│   ├── general.py
│   ├── migration.py
│   └── capacity.py
└── static/
    ├── index.html
    ├── style.css
    ├── base.js
    ├── stacks.js
    └── migrations.js
```

## Požadavky

- Docker Engine + Docker Compose
- Portainer s API klíčem s přístupem k požadovaným endpointům/stackům
- dostupné Portainer endpointy
- cílové endpointy explicitně povolené v rozhraní Moveru

## Instalace

```bash
git clone https://github.com/Drbanek/DC1-mover.git
cd DC1-mover
cp .env.example .env
```

Upravte `.env`, zejména `PORTAINER_URL`, `PORTAINER_TOKEN`, `MOVER_PASSWORD` a `MOVER_SESSION_SECRET`.

```bash
docker compose up -d --build
```

Výchozí konfigurace publikuje rozhraní na `127.0.0.1:8081`. Pomocí `MOVER_BIND_IP` lze nastavit adresu, na které má služba poslouchat.

Podrobný český návod pro build, GHCR, Portainer a migrace je v [docs/KOMPILACE-DOCKER.md](docs/KOMPILACE-DOCKER.md).

## Nastavení endpointů

Po přihlášení povolte pouze Docker endpointy, které mají být součástí migračního poolu. Názvy endpointů jsou pouze informativní a mohou být libovolné.

U běžného `tcp://host:port` endpointu Mover zjistí Host IP automaticky. U Portainer Agent, Edge, DNS nebo jiného typu lze Host IP zadat ručně.

## Průběh migrace

Mover zastaví zdrojový stack, přenese named volumes, vytvoří stack na cíli, ověří cílové kontejnery a původní stack ponechá zastavený pro případný rollback. Teprve explicitní potvrzení migrace odstraní původní kopii.

## Migrace mezi lokalitami

Endpointy nemusí být ve stejné LAN. Mohou být propojené privátní sítí/VPN nebo vhodně zabezpečeným veřejným spojením. Pro nezávislé lokality je vhodné mít v každé lokalitě vlastní reverse proxy.

Verze 1.0.0 automaticky nemění veřejné DNS. DNS integrace pro cross-site migrace je plánována jako další rozšíření.

## Bezpečnost

Nikdy neukládejte `.env`, Portainer API token, heslo ani session secret do repozitáře. `MOVER_SESSION_SECRET` musí mít alespoň 32 znaků. Aktuální verze používá pro spojení s Portainerem `verify=False`; Mover proto provozujte v důvěryhodné management síti, dokud nebude TLS ověřování upraveno.

## Verze 1.0.0

v1.0.0 představuje otestovaný základ migračního enginu: migrace stacku a persistentních volumes, Host IP rewrite, pre-flight kontroly, ověření cíle, historie, potvrzení migrace a rollback.

## Licence

MIT License — Copyright (c) 2026 Lukáš Kačírek.
