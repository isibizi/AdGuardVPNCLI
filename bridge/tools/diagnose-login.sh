#!/bin/sh
#
# Findet heraus, warum die AdGuard-Anmeldung einen Containerstart nicht überlebt.
#
#   curl -fsSL .../bridge/tools/diagnose-login.sh | sh
#
# Voraussetzung: im Panel angemeldet. Das Skript prüft das selbst.
# Es fasst nur Testdaten an, sichert die Konfiguration vorher und startet die
# Bridge am Ende wieder.

set -u

DIR="${INSTALL_DIR:-/opt/adguard-bridge}"
CONF_REL='data/adguard/adguardvpn-cli/adguardvpn-cli.conf'
BACKUP='/tmp/adguard-conf.good'
LOGGED_IN_MIN=300

cd "$DIR" || { echo "FEHLER: $DIR nicht gefunden"; exit 1; }

IMAGE="$(docker compose config --images 2>/dev/null | head -1)"
[ -n "$IMAGE" ] || IMAGE='ghcr.io/isibizi/adguard-wg-bridge:latest'

# Function size prints the configuration file size in bytes.
size() {
  wc -c < "$CONF_REL" 2>/dev/null | tr -d ' '
}

# Function probe runs one throwaway container on the same volume and reports the
# file size after each startup step, so the step that wipes the token names
# itself. No capabilities are needed - none of these steps touch the network.
probe() {
  docker run --rm -v "${DIR}/data:/data" -e XDG_DATA_HOME=/data/adguard \
    -e PYTHONPATH=/opt/panel --entrypoint sh "$IMAGE" -c "$1" 2>&1
}

echo "=== Voraussetzung ==="
start_size="$(size)"
echo "Konfiguration: ${start_size:-0} Bytes"
if [ "${start_size:-0}" -lt "$LOGGED_IN_MIN" ]
then
  echo
  echo "ABBRUCH: Das sieht nach 'nicht angemeldet' aus."
  echo "Bitte im Panel bei AdGuard anmelden und das Skript erneut ausführen."
  exit 1
fi

cp "$CONF_REL" "$BACKUP"
echo "Gesichert nach $BACKUP"

echo
echo "=== Bridge anhalten ==="
docker compose stop >/dev/null 2>&1
echo "angehalten, Datei unverändert: $(size) Bytes"

echo
echo "=== Test 1: Startschritte einzeln ==="
cp "$BACKUP" "$CONF_REL"
probe '
C=/data/adguard/adguardvpn-cli/adguardvpn-cli.conf
s() { wc -c < "$C" | tr -d " "; }
echo "  0 frischer Container    : $(s)"
echo "    Reste im Verzeichnis  : $(ls /data/adguard/adguardvpn-cli | tr "\n" " ")"
mkdir -p /data/adguard /data/wg 2>/dev/null; chmod 700 /data /data/adguard /data/wg 2>/dev/null
echo "  1 mkdir + chmod         : $(s)"
python3 -m app.cli init >/dev/null 2>&1
echo "  2 app.cli init          : $(s)"
adguardvpn-cli status >/dev/null 2>&1
echo "  3 erster CLI-Aufruf     : $(s)"
adguardvpn-cli config set-mode SOCKS >/dev/null 2>&1
echo "  4 config set-mode       : $(s)"
'

echo
echo "=== Test 2: dasselbe ohne toten vpn.socket ==="
cp "$BACKUP" "$CONF_REL"
rm -f data/adguard/adguardvpn-cli/vpn.socket data/adguard/adguardvpn-cli/vpn.pid
probe '
C=/data/adguard/adguardvpn-cli/adguardvpn-cli.conf
echo "  vorher                  : $(wc -c < "$C" | tr -d " ")"
adguardvpn-cli status >/dev/null 2>&1
echo "  nach erstem CLI-Aufruf  : $(wc -c < "$C" | tr -d " ")"
'

echo
echo "=== Test 3: nur der Client, ganz ohne meine Startschritte ==="
cp "$BACKUP" "$CONF_REL"
probe '
C=/data/adguard/adguardvpn-cli/adguardvpn-cli.conf
echo "  vorher                  : $(wc -c < "$C" | tr -d " ")"
adguardvpn-cli license >/dev/null 2>&1
echo "  nach license            : $(wc -c < "$C" | tr -d " ")"
'

echo
echo "=== Bridge wieder starten ==="
cp "$BACKUP" "$CONF_REL"
docker compose start >/dev/null 2>&1
sleep 12
echo "Konfiguration: $(size) Bytes"
printf 'Status:        '
docker compose exec -T bridge adguardvpn-cli status 2>/dev/null | head -1

echo
echo "=== Deutung ==="
echo "Die Zeile in Test 1, bei der die Byte-Zahl von ~${start_size} auf ~182 fällt,"
echo "benennt den Schuldigen. Bleibt sie in Test 2 oben, war der tote Socket schuld."
echo "Fällt sie schon in Test 3, liegt es allein am Client und nicht an meinem Start."
