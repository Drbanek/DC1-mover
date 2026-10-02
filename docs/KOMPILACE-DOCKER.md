# Kompilace Dockeru, nasazení a migrace

**Čeština** | [English](DOCKER-BUILD-DEPLOY.en.md)

Tento dokument popisuje kompletní postup pro **Portainer Stack Mover v1.0.0** – build aplikace, publikaci Docker image do GHCR, nasazení přes Portainer, konfiguraci Docker endpointů a bezpečnou migraci stacku včetně rollbacku.

> Cíl v1.0.0 je záměrně úzký: spolehlivě přenést Docker Compose/Portainer stack a jeho named volumes mezi vybranými Docker endpointy. Automatizace DNS a přepínání provozu mezi lokalitami budou řešeny samostatně.

## 1. Architektura

```text
Internet
   |
   +-- Lokalita A / veřejná IP
   |     +-- Reverse proxy
   |     +-- Docker NODE01
   |     +-- Docker NODE02
   |
   +-- Lokalita B / veřejná IP
         +-- Reverse proxy
         +-- Docker NODE01
         +-- Docker NODE02

Management
   +-- Portainer
   +-- Portainer Stack Mover
```

Názvy endpointů jsou libovolné. Aplikace nevyžaduje pojmenování typu `DC1-NODE01`. Endpointy určené pro migrace se explicitně povolují v rozhraní Moveru.

Docker hosty mohou být ve stejné LAN nebo v jiné lokalitě. Propojení může být přes privátní routovanou síť/VPN nebo vhodně zabezpečenou veřejnou konektivitu.

## 2. Požadavky

- Docker Engine na cílových hostech
- Portainer s přidanými Docker endpointy
- Portainer API token
- GitHub repozitář projektu
- GitHub Container Registry (GHCR)
- síťová dostupnost Portaineru a Moveru
- Docker named volumes pro persistentní data

Mover nepotřebuje SSH přístup na jednotlivé Docker nody.

## 3. Build Docker image

Image sestavují GitHub Actions, nikoliv Portainer. Produkční nasazení používá předem sestavený image z GHCR:

```text
ghcr.io/drbanek/dc1-mover:latest
```

CI publikuje také image svázaný s konkrétním commitem. Tím je možné nasazení přesně reprodukovat.

## 4. GitHub Actions / GHCR

```text
git push
   |
   v
GitHub Actions
   |
   +-- kontrola / build
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

Build tak neprobíhá na produkčních Docker nodech. Portainer pouze stáhne hotový image.

## 5. Nasazení přes Portainer

Doporučený způsob je **Git repository stack**.

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

Potřebné proměnné:

```text
PORTAINER_URL=https://<portainer-address>:9443
PORTAINER_TOKEN=<secret>
MOVER_USER=<username>
MOVER_PASSWORD=<secret>
MOVER_SESSION_SECRET=<secret>
MOVER_BIND_IP=<adresa>
MOVER_PORT=8081
```

Tajné údaje nikdy neukládejte do repozitáře.

Persistentní stav Moveru je v Docker volume `mover-data:/data`. Nastavení endpointů a historie migrací proto přežijí redeploy.

## 6. Aktualizace

Po úspěšném publikování nového image do GHCR:

1. otevřete stack Moveru v Portaineru;
2. použijte **Pull and redeploy**;
3. Portainer stáhne aktuální image;
4. kontejner se vytvoří znovu;
5. persistentní volume `mover-data` zůstane zachováno.

Pro produkční release lze místo `latest` použít pevný tag.

## 7. Nastavení endpointů

V Moveru otevřete **Nastavení Docker endpointů**.

U endpointů, které se mají účastnit migrací:

- zapněte **Povolit migrace**;
- podle potřeby nastavte **Host IP**;
- volitelně vyplňte **Lokalita / skupina**;
- změny uložte.

Pouze povolené endpointy jsou součástí migračního poolu a inventáře stacků.

U běžného `tcp://host:port` endpointu lze Host IP zjistit automaticky. U Agent/Edge/DNS nebo jiného typu ji lze zadat ručně.

## 8. Průběh migrace

1. Mover načte definici stacku a metadata z Portaineru.
2. Zjistí používané named volumes.
3. Provede pre-flight cíle.
4. Zkontroluje kolize stacku, volumes a publikovaných portů.
5. Zastaví zdrojový stack.
6. Přenese persistentní data volumes.
7. Vytvoří cílové volumes.
8. Obnoví data na cíli.
9. Podle potřeby přepíše Host IP publikovaných portů.
10. Vytvoří a spustí stack na cílovém endpointu.
11. Ověří cílové kontejnery/health.
12. Původní stack ponechá zastavený pro případ rollbacku.

Zdroj se po úspěšné migraci automaticky nemaže.

## 9. Potvrzení a rollback

Po ověření aplikace na cíli lze migraci explicitně potvrdit. Teprve tím se odstraní původní kopie podle migračního workflow.

Při rollbacku se odstraní cílová migrovaná kopie a znovu se spustí zachovaný zdrojový stack.

## 10. Migrace mezi lokalitami

Mover není omezen na jednu podsíť. Cílový endpoint může být v jiném datacentru nebo lokalitě, pokud funguje potřebná komunikace přes Portainer/Docker.

Preferovaná varianta je VPN nebo privátní management síť. Při použití veřejné IP musí firewall povolit pouze nezbytné zdroje a služby.

Každá nezávislá lokalita by měla mít vlastní reverse proxy:

```text
app.example.com -> veřejná IP A -> Proxy A -> Docker A
```

Po přesunu:

```text
app.example.com -> veřejná IP B -> Proxy B -> Docker B
```

v1.0.0 přesouvá workload a data, ale automaticky nemění veřejné DNS.

## 11. DNS

Při ruční cross-site migraci:

1. předem snižte TTL;
2. připravte proxy v cílové lokalitě;
3. proveďte migraci;
4. ověřte cílovou aplikaci;
5. změňte A/AAAA záznam na veřejnou IP cílové lokality;
6. ověřte DNS, HTTPS a aplikaci;
7. teprve poté migraci potvrďte.

Při rollbacku vraťte původní DNS a proveďte rollback stacku.

Budoucí verze má počítat s volitelnou vrstvou DNS providerů. První plánovaná integrace je **Váš Hosting DNS API**.

## 12. Bezpečnost

- omezte přístup k Moveru firewallem;
- používejte HTTPS/reverse proxy;
- chraňte Portainer API token;
- chraňte heslo a session secret;
- tajné údaje nikdy necommitujte do Gitu;
- mezi lokalitami preferujte VPN/privátní routing;
- nevystavujte Docker/Portainer Agent management porty volně do internetu;
- před první produkční migrací mějte zálohu.

Aktuální verze používá pro Portainer TLS `verify=False`; to je důvod navíc držet management službu v důvěryhodné síti.

## 13. Rozsah v1.0.0

- libovolné názvy Portainer endpointů
- explicitní výběr endpointů pro migrace
- persistentní konfigurace endpointů
- inventář stacků pouze z vybraných endpointů
- pre-flight kontroly
- přenos named volumes
- Host IP rewrite
- ověření cíle
- historie migrací
- explicitní potvrzení
- otestovaný rollback
- build/publikace přes GHCR
- nasazení jako Portainer Git stack

v1.0.0 je stabilní základ migračního enginu. Další integrace mají být oddělené a nemají zbytečně měnit ověřené migrační jádro.
