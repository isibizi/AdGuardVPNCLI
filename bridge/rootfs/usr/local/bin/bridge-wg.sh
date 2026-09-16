#!/bin/sh
#
# WireGuard server management for the bridge.
#
#   bridge-wg.sh up      create wg0 and load the configuration
#   bridge-wg.sh sync    apply configuration changes without dropping peers
#   bridge-wg.sh down    remove wg0
#
# The configuration is rendered from the panel's database by the Python helper
# `app.cli render-wg`, which writes a `wg setconf` style file plus the list of
# extra networks that live behind peers. wg-quick is deliberately not used: it
# would install its own default routes and fight with the policy routing set up
# by bridge-net.sh.

set -eu

WG_IF='wg0'
DATA_DIR="${BRIDGE_DATA_DIR:-/data}"
WG_DIR="${DATA_DIR}/wg"
CONF="${WG_DIR}/wg0.conf"
ROUTES="${WG_DIR}/routes.txt"
PYTHON='/opt/panel-venv/bin/python3'

WG_MTU="${WG_MTU:-1380}"

log() {
  echo "[bridge-wg] $1" >&2
}

# Function bridge_addr prints the bridge's own address inside the tunnel,
# i.e. the first host of WG_SUBNET (10.8.0.1 by default).
bridge_addr() {
  WG_SUBNET="${WG_SUBNET:-10.8.0.0/24}" "$PYTHON" - <<'PY'
import ipaddress
import os

net = ipaddress.ip_network(os.environ['WG_SUBNET'], strict=False)
print(f"{next(net.hosts())}/{net.prefixlen}")
PY
}

# Function render regenerates the interface configuration from the database.
render() {
  mkdir -p "$WG_DIR"
  "$PYTHON" -m app.cli render-wg --config "$CONF" --routes "$ROUTES"
}

# Function create_link brings up the wg0 interface, falling back to the
# userspace implementation when the host has no WireGuard kernel module (common
# on LXC/OpenVZ VPS offerings).
create_link() {
  if ip link show "$WG_IF" >/dev/null 2>&1
  then
    return 0
  fi

  if ip link add "$WG_IF" type wireguard 2>/dev/null
  then
    log 'using kernel WireGuard'
    echo 'kernel' > "${WG_DIR}/backend"
    return 0
  fi

  if command -v wireguard-go >/dev/null 2>&1
  then
    log 'kernel module unavailable, falling back to userspace wireguard-go'
    WG_QUICK_USERSPACE_IMPLEMENTATION=wireguard-go wireguard-go "$WG_IF"
    echo 'userspace' > "${WG_DIR}/backend"
    return 0
  fi

  echo 'userspace-missing' > "${WG_DIR}/backend"
  log 'ERROR: no WireGuard kernel module and wireguard-go is not installed'
  return 1
}

# Function apply_routes installs routes for the networks that sit behind peers
# (for example the LAN behind the UniFi gateway), so replies find their way back
# into the tunnel. Routes that are no longer wanted are removed again.
apply_routes() {
  [ -f "$ROUTES" ] || return 0

  while IFS= read -r net
  do
    [ -n "$net" ] || continue
    ip route replace "$net" dev "$WG_IF"
  done < "$ROUTES"

  # Drop routes on wg0 that are not in the file any more.
  ip route show dev "$WG_IF" | awk '{print $1}' | while IFS= read -r existing
  do
    [ -n "$existing" ] || continue
    case "$existing" in
      "${WG_SUBNET:-10.8.0.0/24}") continue ;;
    esac
    if ! grep -qx "$existing" "$ROUTES" 2>/dev/null
    then
      ip route del "$existing" dev "$WG_IF" 2>/dev/null || true
    fi
  done
}

case "${1:-}" in
'up')
  render
  create_link
  wg setconf "$WG_IF" "$CONF"

  addr="$(bridge_addr)"
  if ! ip -4 addr show dev "$WG_IF" | grep -q "${addr%/*}"
  then
    ip addr add "$addr" dev "$WG_IF"
  fi
  ip link set dev "$WG_IF" up mtu "$WG_MTU"
  apply_routes
  log "up on ${addr} (mtu ${WG_MTU})"
  ;;

'sync')
  render
  # syncconf updates peers in place: existing sessions keep their handshake.
  wg syncconf "$WG_IF" "$CONF"
  apply_routes
  log 'configuration synced'
  ;;

'down')
  ip link del "$WG_IF" 2>/dev/null || true
  log 'down'
  ;;

*)
  echo "usage: $0 up|sync|down" >&2
  exit 2
  ;;
esac
