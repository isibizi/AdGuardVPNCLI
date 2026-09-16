# AdGuard VPN → WireGuard Bridge für UniFi

AdGuard VPN bietet **kein WireGuard und kein OpenVPN** an. Der offizielle CLI-Client
spricht nur AdGuards eigene Protokolle (HTTP/2, QUIC) oder stellt einen lokalen
SOCKS5-Proxy bereit. Ein UniFi-Gateway kann aber ausschließlich WireGuard, OpenVPN
oder IPsec als VPN-Client.

Diese Bridge schließt genau diese Lücke. Sie läuft als ein einziger Docker-Container
auf einem kleinen VPS:

```
UniFi Gateway ──WireGuard udp/51820──▶  VPS-Bridge  ──▶ AdGuard VPN ──▶ Internet
    (dein LAN)                          │
                                        │  wg0 ─ Policy-Route ─▶ tun0
                                        │                         │
                                        │                     tun2socks
                                        │                         │
                                        └──── adguardvpn-cli (SOCKS5 :1080)
```

Bedient wird alles über ein Browser-Panel: AdGuard-Anmeldung, Standortwahl, Status,
Geräteverwaltung mit Konfigurations-Download und QR-Code, Bandbreiten-Diagramm und
Ereignisprotokoll.

---

## Was du brauchst

| | |
|---|---|
| **VPS** | 1 vCPU, 1 GB RAM reichen. **KVM-Virtualisierung** (nicht LXC/OpenVZ), Kernel ≥ 5.6, damit WireGuard im Kernel läuft. Debian 12 oder Ubuntu 22.04/24.04. |
| **Docker** | Docker Engine mit dem `compose`-Plugin. |
| **AdGuard VPN** | Ein aktives Abo. Der VPS zählt als **ein** Gerät – egal wie viele Geräte in deinem LAN dahinter hängen. |
| **UniFi** | Ein UniFi-OS-Gateway (UDM, UDM-Pro, UDR, UX, UCG) mit WireGuard-VPN-Client. Der alte USG kann kein WireGuard. |
| **Firewall des VPS** | UDP-Port 51820 muss von außen erreichbar sein. Sonst nichts. |

---

## Installation

### Empfohlen: fertiges Image

Du brauchst weder Git noch das Repository – zwei Dateien reichen:

```bash
mkdir -p /opt/adguard-bridge && cd /opt/adguard-bridge

curl -fsSLO https://raw.githubusercontent.com/isibizi/AdGuardVPNCLI/master/bridge/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/isibizi/AdGuardVPNCLI/master/bridge/.env.example -o .env

nano .env          # mindestens WG_ENDPOINT und PANEL_PASSWORD setzen
docker compose up -d
```

`WG_ENDPOINT` ist die öffentliche IP oder der DNS-Name deines VPS – der Wert landet
in jeder Client-Konfiguration. Ohne ihn funktionieren die heruntergeladenen Configs
nicht.

Das Image wird für `linux/amd64` und `linux/arm64` gebaut und liegt unter
`ghcr.io/isibizi/adguard-wg-bridge`. Standardmäßig wird `latest` gezogen; für einen
festen Stand setzt du in der `.env` z. B. `BRIDGE_TAG=sha-1a2b3c4`.

> **Einmalig nötig, falls der Pull mit `denied` oder `not found` scheitert:** GHCR-Pakete
> sind anfangs privat. Unter
> `https://github.com/users/isibizi/packages/container/adguard-wg-bridge/settings`
> die Sichtbarkeit auf *public* stellen. Alternativ auf dem Server einmal
> `echo <token> | docker login ghcr.io -u isibizi --password-stdin` mit einem Token
> mit `read:packages`.

### Alternative: selbst bauen

Wenn du den Code lieber selbst übersetzt oder Änderungen testen willst:

```bash
git clone https://github.com/isibizi/AdGuardVPNCLI.git
cd AdGuardVPNCLI/bridge
cp .env.example .env
nano .env
docker compose -f docker-compose.build.yml up -d --build
```

Der Build dauert beim ersten Mal ein paar Minuten: Er lädt den offiziellen AdGuard-Client
über `scripts/release/install.sh` aus diesem Repository sowie `tun2socks` herunter.

Damit du das `-f` nicht jedes Mal tippen musst, kannst du in die `.env` schreiben:
`COMPOSE_FILE=docker-compose.build.yml`.

## Ersteinrichtung

Das Panel ist bewusst **nicht** ins Internet veröffentlicht. Für die Ersteinrichtung
baust du einen SSH-Tunnel auf – da existiert noch kein WireGuard-Gerät:

```bash
ssh -L 8080:localhost:8080 root@DEINE-VPS-IP
```

Dann im Browser `http://localhost:8080` öffnen. Der Assistent führt durch vier Schritte:

1. **Panel-Passwort vergeben** (oder vorher in der `.env` setzen).
2. **Bei AdGuard anmelden.** AdGuard hat den Login mit Benutzername und Passwort in
   Version 1.5.10 abgeschafft. Das Panel erzeugt stattdessen einen Anmeldelink mit
   Einmal-Code und zeigt ihn als Button, als Text und als QR-Code. Dein AdGuard-Passwort
   wird hier nie eingegeben und nie gespeichert. Die Sitzung liegt danach in `./data` und
   überlebt Neustarts.
3. **Standort wählen.** Durchsuchbare Liste aller AdGuard-Standorte mit Ping, oder
   „schnellster Standort“ automatisch.
4. **Gerät anlegen.** Für das UniFi-Gateway; Konfiguration herunterladen.

## UniFi einrichten

1. **Settings → VPN → VPN Client → Create New → WireGuard**, die heruntergeladene
   `.conf` hochladen.
2. **Settings → Routing → Traffic Routes**: eine Route `0.0.0.0/0` anlegen, als Ziel
   den eben erstellten VPN-Client wählen und dein Netzwerk zuweisen. Damit läuft das
   komplette LAN über die Bridge. (Du kannst hier später jederzeit einschränken, wenn
   doch nur einzelne Geräte oder ein VLAN darüber sollen.)
3. **DNS setzen.** UniFi ignoriert die `DNS=`-Zeile importierter Configs. Trage
   `94.140.14.14` und `94.140.15.15` in den Netzwerkeinstellungen ein, sonst fragst du
   weiter den DNS deines Providers – ein klassisches Leck.
4. **UniFi-Kill-Switch aktivieren.** Er ergänzt den auf dem VPS, siehe unten.
5. **Optional: das LAN beim Peer hinterlegen.** Trägst du im Panel beim Peer dein
   LAN-Netz ein (z. B. `192.168.1.0/24`), finden Antwortpakete an deine LAN-Geräte
   zurück in den Tunnel. Nur dann erreichst du das Panel auch von einem normalen
   LAN-Client.

## Das Panel später erreichen

Sobald das erste Gerät verbunden ist, brauchst du den SSH-Tunnel nicht mehr:

**http://10.8.0.1:8080** – von jedem verbundenen WireGuard-Gerät und, bei hinterlegtem
LAN-Netz, aus dem Netz hinter dem UniFi.

Das gilt auch dann, wenn die AdGuard-Strecke gerade unterbrochen ist: Der Kill-Switch
blockiert nur *weitergeleiteten* Verkehr, nicht den Zugriff auf die Bridge selbst.
Genau dann brauchst du das Panel ja.

---

## Der Kill-Switch

Er ist **fest eingebaut und nicht abschaltbar**. Es gibt keine Option dafür.

Die Kette hat zwei Beine, und beide brauchen ihren eigenen Schutz:

| Ausfall | Wer merkt es | Was passiert |
|---|---|---|
| VPS nicht erreichbar, WireGuard tot | UniFi-Kill-Switch | UniFi blockiert das LAN |
| AdGuard-Verbindung weg, WireGuard gesund | **nur die Bridge** | Die Bridge verwirft den Verkehr |

Der zweite Fall ist der gefährliche: WireGuard läuft dann völlig normal weiter,
Handshakes kommen an, UniFi meldet „verbunden“ und schickt fleißig Daten. Ohne die
Regeln auf dem VPS würde dieser Verkehr über die nackte VPS-IP ins Internet gehen –
unbemerkt, weil auf der UniFi-Seite alles grün bleibt.

Technisch: `FORWARD`-Policy ist `DROP`, es gibt genau einen erlaubten Pfad
(`wg0 → tun0`), und dieser wird vom Watchdog erst freigegeben, wenn er nachgewiesen hat,
dass wirklich Verkehr durch AdGuard fließt. Dazu eine explizite `DROP`-Regel für
`wg0 → eth0`. Verworfen wird still (`DROP`, nicht `REJECT`), damit kurze Reconnects
bestehende TCP-Verbindungen nicht abreißen lassen.

## Automatischer Wiederaufbau

Bricht die Verbindung zwischen VPS und AdGuard ab, baut der Watchdog sie ohne Zutun
wieder auf:

- Er prüft alle 15 Sekunden den Status **und misst zusätzlich jede Minute aktiv**, ob
  wirklich Daten durchkommen. Der Client meldet gelegentlich „connected“, obwohl der
  Tunnel tot ist – dieser Zombie-Zustand wird nur durch die aktive Messung erkannt.
- Beim Wiederaufbau: `disconnect`, `connect`, warten bis der SOCKS-Proxy lauscht,
  `tun2socks` neu starten, Durchlass wieder öffnen.
- Wartezeiten 5 s → 10 s → 20 s → 40 s → danach dauerhaft jede Minute. **Er gibt nie auf.**
- Ist der gewählte Standort mehrfach nicht erreichbar, weicht er einmal auf den
  schnellsten aus und vermerkt das im Protokoll (`FALLBACK_TO_FASTEST=false` schaltet
  das ab).
- Nur bei abgelaufener Sitzung hilft kein Reconnect: Dann drosselt er auf einen
  5-Minuten-Takt und das Panel zeigt gut sichtbar „Neu anmelden“.
- Container-Neustart und VPS-Reboot verbinden ebenfalls automatisch.

Jedes Ereignis landet im Protokoll unter **Monitor → Ereignisse** und in
`data/events.log`.

## Monitor

- **Bandbreite** – Live-Diagramm für Download und Upload, umschaltbar von 10 Minuten
  bis 30 Tage. Gemessen am WireGuard-Interface.
- **Verbundene Geräte** – welches Gerät gerade online ist (letzter Handshake jünger als
  drei Minuten), von welcher IP es sich verbindet, aktuelle Rate, Volumen heute und im
  Monat.
- **Aktive Quellen** – siehst du hier nur `10.8.0.2` statt einzelner LAN-Geräte, dann
  NATet dein UniFi den Verkehr in den Tunnel. Das ist der Normalfall und kein Fehler.
- **Ereignisse** – filterbar, als Textdatei herunterladbar.

Ein Detailprotokoll einzelner Verbindungen **mit Zieladressen** ist bewusst
abgeschaltet (`CONNECTION_LOG=false`) – es wäre faktisch ein Surf-Protokoll deines
Haushalts.

---

## Fehlersuche

**Teste nie mit `ping`.** `tun2socks` beantwortet ICMP selbst; ein erfolgreiches `ping
1.1.1.1` beweist gar nichts. Nimm eine echte TCP-Verbindung, etwa eine Webseite oder:

```bash
curl https://api.ipify.org
```

Die angezeigte Adresse muss von der IP deines VPS abweichen. Genau diesen Vergleich
zeigt auch das Dashboard.

| Symptom | Ursache und Lösung |
|---|---|
| Kein Internet im LAN, Dashboard zeigt „Blockiert“ | Der Kill-Switch arbeitet korrekt – die AdGuard-Strecke steht nicht. Ins Panel schauen: meist ist eine Neuanmeldung fällig. |
| Exit-IP **ist gleich** der VPS-IP | Der Verkehr läuft an AdGuard vorbei. Sofort im Panel prüfen; normalerweise kann das nicht passieren, weil der Durchlass sonst zu wäre. |
| Manche Webseiten laden nicht, Downloads brechen ab | MTU. `WG_MTU` in der `.env` auf `1280` senken und `docker compose up -d` erneut ausführen. |
| Container startet nicht, Log sagt „no WireGuard kernel module“ | Dein VPS ist LXC/OpenVZ. Der Userspace-Fallback (`wireguard-go`) springt ein, ist aber langsamer. Besser: KVM-VPS. |
| DNS-Anfragen gehen am VPN vorbei | In UniFi den DNS-Server auf die AdGuard-Adressen setzen (siehe oben). UniFi ignoriert die `DNS=`-Zeile der Config. |
| Anmeldelink lässt sich nicht öffnen | Dein LAN läuft über die tote Bridge. Den QR-Code mit dem Handy über **Mobilfunk** scannen, oder in UniFi die Traffic Route kurz deaktivieren. |
| Standort wechselt nicht | Nach dem Speichern dauert es bis zu 15 Sekunden, bis der Watchdog neu verbindet. Das Ereignisprotokoll zeigt den Verlauf. |

Rohausgaben des Clients gibt es auf dem Dashboard unter **Diagnose**, ausführliche Logs mit:

```bash
docker compose logs -f bridge
docker compose exec bridge cat /data/events.log
docker compose exec bridge /usr/local/bin/bridge-net.sh status
docker compose exec bridge wg show
```

---

## Wartung

**Daten sichern.** Alles Wichtige liegt in `./data`: die AdGuard-Sitzung, die
WireGuard-Serverschlüssel, die Gerätedatenbank (inklusive privater Schlüssel, damit du
Konfigurationen erneut herunterladen kannst) und das Panel-Passwort. Diese Datenbank
ist `0600` – behandle sie wie einen Schlüsselbund.

**Aktualisieren.** Beim fertigen Image:

```bash
cd /opt/adguard-bridge
docker compose pull && docker compose up -d
```

Beim Selbstbauen entsprechend `git pull && docker compose -f docker-compose.build.yml up -d --build`.

Die Version des AdGuard-Clients kommt aus `scripts/release/install.sh`, das in diesem
Repository automatisch mit den Upstream-Releases mitgepflegt wird – ein neuer Build
holt also automatisch die aktuelle Version.

**tun2socks aktualisieren.** Version und SHA256-Prüfsummen stehen als Build-Args im
`Dockerfile` und sind gepinnt; ein fehlender Wert lässt den Build absichtlich
fehlschlagen, statt eine ungeprüfte Binärdatei zu installieren. Beim Versionswechsel
beide Summen neu bestimmen:

```bash
for arch in amd64 arm64; do
  curl -fsSLO "https://github.com/xjasonlyu/tun2socks/releases/download/v2.6.0/tun2socks-linux-${arch}.zip"
  sha256sum "tun2socks-linux-${arch}.zip"
done
```

**Tests ausführen** (Entwicklung):

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

---

## Sicherheit und Grenzen

- Das Panel ist nur über `127.0.0.1` und über den WireGuard-Tunnel erreichbar, nicht
  aus dem Internet. Es ist trotzdem passwortgeschützt, denn jedes Gerät im LAN hinter
  dem UniFi kommt daran.
- **IPv6 ist auf der ganzen Strecke abgeschaltet.** `tun2socks` transportiert es nicht;
  wäre es aktiv, liefe IPv6-Verkehr am VPN vorbei. Die erzeugten Configs enthalten
  deshalb bewusst kein `::/0`.
- Der SOCKS-Proxy lauscht ausschließlich auf `127.0.0.1` und ist von den Peers aus nicht
  erreichbar.
- Gedacht ist das für **deinen eigenen AdGuard-Account und deine eigenen Geräte**. Die
  Weitergabe des Zugangs an Dritte ist nicht der Zweck dieser Bridge.

## Hinweis zum Repository

Dieses Verzeichnis ist eine Ergänzung zum Fork des AdGuard-Tracker-Repositorys. Die
Dateien unter `scripts/` werden von einem GitHub-Workflow automatisch mit den
Upstream-Releases synchronisiert und dürfen **nicht** von Hand geändert werden – der
Docker-Build benutzt `scripts/release/install.sh` nur.

Der Workflow `.github/workflows/bridge-image.yml` baut das Container-Image und
veröffentlicht es nach GHCR. Er reagiert ausschließlich auf Änderungen unter `bridge/`
und fasst die Release-Workflows des Forks nicht an.
