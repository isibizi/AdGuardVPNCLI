#!/bin/sh
#
# One-shot installer for the AdGuard VPN -> WireGuard bridge.
#
#   curl -fsSL https://raw.githubusercontent.com/isibizi/AdGuardVPNCLI/master/install-on-vps.sh | sh
#
# Checks the prerequisites, installs Docker if it is missing, writes a .env with
# a generated panel password, opens the WireGuard port and starts the container.
#
# Safe to re-run: an existing .env is never overwritten, so a second run is an
# update (pull the new image and restart) rather than a fresh install.
#
# The public endpoint is detected automatically. Override it when the server is
# behind NAT or you want a DNS name instead:
#
#   WG_ENDPOINT=vpn.example.org sh install-on-vps.sh
#   sh install-on-vps.sh vpn.example.org

set -e -u

INSTALL_DIR="${INSTALL_DIR:-/opt/adguard-bridge}"
RAW_BASE='https://raw.githubusercontent.com/isibizi/AdGuardVPNCLI/master/bridge'
WG_PORT_DEFAULT='51820'

# Function say prints a step heading.
say() {
  echo ""
  echo "==> $1"
}

# Function fail prints an error and stops.
fail() {
  echo "FEHLER: $1" >&2
  exit 1
}

# Function usage explains how to call the script.
usage() {
  echo "Aufruf: sh install-on-vps.sh [endpoint]" >&2
  echo "  endpoint  Öffentliche IP oder DNS-Name des VPS (sonst automatisch erkannt)" >&2
  exit 0
}

case "${1:-}" in
  '-h'|'--help') usage ;;
esac

[ "$(id -u)" = '0' ] || fail 'Bitte als root ausführen (oder mit sudo).'

# --- endpoint ---------------------------------------------------------------
endpoint="${1:-${WG_ENDPOINT:-}}"
if [ -z "$endpoint" ]
then
  endpoint="$(curl -fsS --max-time 15 https://api.ipify.org 2>/dev/null || true)"
  [ -n "$endpoint" ] || fail 'Öffentliche IP nicht erkannt. Bitte angeben: sh install-on-vps.sh <ip-oder-dns>'
fi

say "WireGuard-Kernelmodul"
modprobe wireguard 2>/dev/null || true
if [ -d /sys/module/wireguard ]
then
  echo "    OK – Kernel-WireGuard verfügbar."
else
  echo "    WARNUNG: kein Kernelmodul gefunden (typisch für LXC/OpenVZ-VPS)."
  echo "    Die Bridge weicht auf den Userspace-Modus aus. Das funktioniert,"
  echo "    ist aber spürbar langsamer – ein KVM-VPS wäre die bessere Basis."
fi

say "Docker"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1
then
  echo "    OK – bereits installiert."
else
  echo "    Wird über das offizielle Skript von get.docker.com installiert."
  curl -fsSL https://get.docker.com | sh
  docker compose version >/dev/null 2>&1 \
    || fail 'Docker wurde installiert, aber das compose-Plugin fehlt.'
fi

say "Dateien nach ${INSTALL_DIR}"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
curl -fsSL -o docker-compose.yml "${RAW_BASE}/docker-compose.yml"
echo "    docker-compose.yml aktualisiert."

say "Konfiguration"
fresh_install='0'
if [ -f .env ]
then
  echo "    Vorhandene .env bleibt unverändert – deine Einstellungen und dein"
  echo "    Panel-Passwort bleiben erhalten."
else
  fresh_install='1'
  curl -fsSL -o .env "${RAW_BASE}/.env.example"

  if command -v openssl >/dev/null 2>&1
  then
    panel_password="$(openssl rand -base64 18)"
  else
    panel_password="$(head -c 18 /dev/urandom | base64)"
  fi

  # The value may contain / and &, so use a delimiter that cannot appear in it.
  sed -i "s|^WG_ENDPOINT=.*|WG_ENDPOINT=${endpoint}|" .env
  sed -i "s|^PANEL_PASSWORD=.*|PANEL_PASSWORD=${panel_password}|" .env
  chmod 600 .env
  echo "    .env angelegt, Endpoint: ${endpoint}"
fi

wg_port="$(sed -n 's/^WG_PORT=\(.*\)$/\1/p' .env)"
[ -n "$wg_port" ] || wg_port="$WG_PORT_DEFAULT"

say "Firewall (${wg_port}/udp)"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'
then
  ufw allow "${wg_port}/udp" >/dev/null && echo "    ufw: freigegeben."
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1
then
  firewall-cmd --permanent --add-port="${wg_port}/udp" >/dev/null
  firewall-cmd --reload >/dev/null
  echo "    firewalld: freigegeben."
else
  echo "    Keine aktive Firewall erkannt."
fi
echo "    Prüfe zusätzlich die Firewall deines Hosters – ${wg_port}/udp muss"
echo "    von außen erreichbar sein."

say "Container starten"
docker compose pull
docker compose up -d
sleep 20
docker compose ps

echo ""
echo "======================================================================"
if [ "$fresh_install" = '1' ]
then
  echo " Panel-Passwort: ${panel_password}"
  echo " (steht auch in ${INSTALL_DIR}/.env)"
  echo ""
fi
echo " Das Panel ist bewusst nicht öffentlich erreichbar. Vom eigenen Rechner:"
echo ""
echo "   ssh -L 8080:localhost:8080 root@${endpoint}"
echo "   danach im Browser: http://localhost:8080"
echo ""
echo " Sobald das erste Gerät per WireGuard verbunden ist, erreichst du das"
echo " Panel direkt unter http://10.8.0.1:8080."
echo ""
echo " Logs: docker compose -f ${INSTALL_DIR}/docker-compose.yml logs -f bridge"
echo "======================================================================"
