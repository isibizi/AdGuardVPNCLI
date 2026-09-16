#!/bin/sh
#
# Container entrypoint: prepare persistent state, arm the kill switch, bring up
# WireGuard, then hand over to supervisor.

set -eu

DATA_DIR="${BRIDGE_DATA_DIR:-/data}"
PYTHON='/opt/panel-venv/bin/python3'
ADGUARD='/opt/adguardvpn_cli/adguardvpn-cli'

log() {
  echo "[bridge] $1" >&2
}

log 'starting AdGuard VPN -> WireGuard bridge'

# --- persistent state ------------------------------------------------------
mkdir -p "${DATA_DIR}/adguard" "${DATA_DIR}/wg"
chmod 700 "${DATA_DIR}" "${DATA_DIR}/adguard" "${DATA_DIR}/wg"

# Keeps the AdGuard session across container rebuilds, so you only log in once.
export XDG_DATA_HOME="${DATA_DIR}/adguard"

if [ -z "${WG_ENDPOINT:-}" ]
then
  log 'WARNING: WG_ENDPOINT is not set. Peer configurations will contain a'
  log '         placeholder endpoint until you set it in .env or in the panel.'
fi

# --- database, server keys, panel secrets ----------------------------------
"$PYTHON" -m app.cli init

# --- AdGuard VPN CLI in SOCKS mode -----------------------------------------
# SOCKS mode is essential: in TUN mode the client installs its own routes,
# which would swallow the WireGuard server's own packets and deadlock the
# tunnel. In SOCKS mode it never touches the routing table.
log 'configuring adguardvpn-cli (SOCKS mode on 127.0.0.1:1080)'
"$ADGUARD" config set-mode SOCKS               >/dev/null 2>&1 || log 'WARN: set-mode failed'
"$ADGUARD" config set-socks-host 127.0.0.1     >/dev/null 2>&1 || log 'WARN: set-socks-host failed'
"$ADGUARD" config set-socks-port 1080          >/dev/null 2>&1 || log 'WARN: set-socks-port failed'
"$ADGUARD" config clear-socks-auth             >/dev/null 2>&1 || true
"$ADGUARD" config set-show-hints off           >/dev/null 2>&1 || true
"$ADGUARD" config set-show-notifications off   >/dev/null 2>&1 || true
"$ADGUARD" config set-crash-reporting off      >/dev/null 2>&1 || true
if [ -n "${ADGUARD_PROTOCOL:-}" ]
then
  "$ADGUARD" config set-protocol "${ADGUARD_PROTOCOL}" >/dev/null 2>&1 || log 'WARN: set-protocol failed'
fi

# --- network ---------------------------------------------------------------
/usr/local/bin/bridge-net.sh up

if ! /usr/local/bin/bridge-wg.sh up
then
  log 'ERROR: could not bring up WireGuard.'
  log '       The host kernel has no WireGuard module and the userspace'
  log '       fallback is unavailable. Use a KVM-based VPS with a kernel >= 5.6.'
  exit 1
fi

# --- shutdown --------------------------------------------------------------
# On stop, take the gate down first so nothing can slip out while the container
# is going away.
trap '/usr/local/bin/bridge-net.sh gate close 2>/dev/null || true' INT TERM

log 'handing over to supervisor'
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
