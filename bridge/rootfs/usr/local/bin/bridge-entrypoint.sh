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
if ! /usr/local/bin/bridge-net.sh up
then
  log 'ERROR: network setup failed. The [bridge-net] lines above say which step.'
  exit 1
fi

if ! /usr/local/bin/bridge-wg.sh up
then
  log 'ERROR: could not bring up WireGuard.'
  log '       The host kernel has no WireGuard module and the userspace'
  log '       fallback is unavailable. Use a KVM-based VPS with a kernel >= 5.6.'
  exit 1
fi

# --- shutdown --------------------------------------------------------------
# The AdGuard service is not a child of supervisor: `adguardvpn-cli connect`
# forks it into the background, so stopping supervisor does not stop it. It was
# therefore left to be SIGKILLed when the container went away - and a killed
# client loses its session, which is why the login had to be repeated after
# every single restart (AdguardTeam/AdGuardVPNCLI#68).
#
# So supervisor runs in the background and this script stays as PID 1 to catch
# the signal and disconnect the client properly first. An `exec` here would
# replace this shell and with it the trap, which is exactly the bug.
shutdown() {
  log 'stopping'
  /usr/local/bin/bridge-net.sh gate close 2>/dev/null || true
  log 'disconnecting AdGuard cleanly so the session survives'
  "$ADGUARD" disconnect >/dev/null 2>&1 || true
  kill -TERM "$supervisor_pid" 2>/dev/null || true
  wait "$supervisor_pid" 2>/dev/null || true
  log 'stopped'
  exit 0
}
trap shutdown INT TERM

log 'handing over to supervisor'
/usr/bin/supervisord -c /etc/supervisor/supervisord.conf &
supervisor_pid="$!"
wait "$supervisor_pid"
