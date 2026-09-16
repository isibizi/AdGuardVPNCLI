#!/bin/sh
#
# Network plumbing for the AdGuard VPN -> WireGuard bridge.
#
#   bridge-net.sh up      set up tun0, policy routing, NAT and the kill switch
#   bridge-net.sh down    tear everything down again
#   bridge-net.sh status  infrastructure health check (used by HEALTHCHECK)
#   bridge-net.sh gate open|close|state
#                         open or close the only path that forwarded traffic
#                         may take; the watchdog flips this based on whether
#                         the AdGuard leg actually works
#
# Every step is idempotent: running `up` twice must not duplicate rules.

set -eu

WG_IF='wg0'
TUN_IF='tun0'
TUN_ADDR='198.18.0.1/15'
RT_TABLE='100'
RT_PREF='100'
PANEL_PORT_INTERNAL='8080'
FWD_CHAIN='BRIDGE_FWD'

WG_SUBNET="${WG_SUBNET:-10.8.0.0/24}"

# Function log writes a timestamped line to stderr.
log() {
  echo "[bridge-net] $1" >&2
}

# Function check_forwarding verifies that IPv4 forwarding is enabled.
#
# It deliberately does not set it. /proc/sys is read-only inside an unprivileged
# container and NET_ADMIN does not change that, so writing it fails with
# "permission denied". The value is applied by docker-compose.yml via `sysctls:`
# at container creation, which is the only moment it can be applied at all. All
# that is left here is to confirm it worked.
check_forwarding() {
  if [ "$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || echo 0)" = '1' ]
  then
    return 0
  fi

  log 'ERROR: IPv4 forwarding is off - nothing can be routed through the bridge.'
  log '       docker-compose.yml sets it under `sysctls:`. Check that the entry'
  log '       is present and recreate the container:'
  log '         docker compose up -d --force-recreate'
  return 1
}

# Function wan_if prints the interface holding the default route, i.e. the way
# out to the internet. Usually eth0, but never assume.
wan_if() {
  ip -4 route show default 2>/dev/null | awk '/default/ {print $5; exit}'
}

# Function ipt_ensure appends a rule only if an identical one is absent.
# Usage: ipt_ensure <table-args...> -- the rule without -A/-C.
ipt_ensure() {
  chain="$1"
  shift
  if ! iptables -C "$chain" "$@" 2>/dev/null
  then
    iptables -A "$chain" "$@"
  fi
}

# Function ipt_remove deletes a rule if present, ignoring absence.
ipt_remove() {
  chain="$1"
  shift
  while iptables -C "$chain" "$@" 2>/dev/null
  do
    iptables -D "$chain" "$@"
  done
}

# Function setup_tun creates the TUN device that tun2socks attaches to. It is
# created here rather than by tun2socks so that routing exists before and
# survives tun2socks restarts.
setup_tun() {
  if ! ip link show "$TUN_IF" >/dev/null 2>&1
  then
    ip tuntap add mode tun dev "$TUN_IF"
    log "created $TUN_IF"
  fi

  if ! ip -4 addr show dev "$TUN_IF" | grep -q "${TUN_ADDR%/*}"
  then
    ip addr add "$TUN_ADDR" dev "$TUN_IF"
  fi

  ip link set dev "$TUN_IF" up mtu 1500
}

# Function setup_routing sends *only* traffic sourced from the WireGuard
# subnet into tun0, via a dedicated routing table. The main table is left
# untouched, so adguardvpn-cli's own upstream connection and the WireGuard
# server's own UDP socket keep using the normal default route. Without this
# separation the tunnel would loop through itself.
setup_routing() {
  ip route replace default dev "$TUN_IF" table "$RT_TABLE"

  if ! ip rule show | grep -q "from ${WG_SUBNET} lookup ${RT_TABLE}"
  then
    ip rule add from "$WG_SUBNET" lookup "$RT_TABLE" pref "$RT_PREF"
    log "policy route for ${WG_SUBNET} -> table ${RT_TABLE}"
  fi
}

# Function setup_firewall installs the kill switch. It is deliberately not
# configurable: there is no option and no code path that lets peer traffic
# reach the internet through the VPS's own address.
#
# The FORWARD policy is DROP and the single ACCEPT for wg0 -> tun0 lives in a
# separate chain that `gate` adds and removes. If the AdGuard leg is down, that
# rule is absent and nothing is forwarded - packets are dropped silently rather
# than rejected, so short reconnects are invisible to TCP instead of tearing
# established connections down.
setup_firewall() {
  wan="$(wan_if)"

  iptables -N "$FWD_CHAIN" 2>/dev/null || true
  ipt_ensure FORWARD -j "$FWD_CHAIN"
  iptables -P FORWARD DROP

  # Return traffic from the VPN back to the peers.
  ipt_ensure "$FWD_CHAIN" -i "$TUN_IF" -o "$WG_IF" \
    -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

  # Belt and braces: even if the policy were changed by accident, peer traffic
  # must never leave via the WAN interface.
  if [ -n "$wan" ]
  then
    ipt_ensure "$FWD_CHAIN" -i "$WG_IF" -o "$wan" -j DROP
  fi

  # Source NAT into the tunnel so tun2socks always sees a consistent source.
  if ! iptables -t nat -C POSTROUTING -s "$WG_SUBNET" -o "$TUN_IF" -j MASQUERADE 2>/dev/null
  then
    iptables -t nat -A POSTROUTING -s "$WG_SUBNET" -o "$TUN_IF" -j MASQUERADE
  fi

  # The control panel must stay reachable from inside the tunnel even while the
  # kill switch is blocking forwarded traffic - that is exactly when you need it
  # to re-authenticate. INPUT is a different chain than FORWARD, so this is
  # unaffected by the gate.
  ipt_ensure INPUT -i "$WG_IF" -p tcp --dport "$PANEL_PORT_INTERNAL" -j ACCEPT
  ipt_ensure INPUT -i "$WG_IF" -p icmp -j ACCEPT
  ipt_ensure INPUT -i "$WG_IF" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
  # Nothing else from the peer network may talk to the VPS itself. The SOCKS
  # proxy only listens on 127.0.0.1 anyway; this makes it explicit.
  ipt_ensure INPUT -i "$WG_IF" -j DROP
}

# Function gate_open allows forwarded peer traffic into the tunnel.
gate_open() {
  ipt_ensure "$FWD_CHAIN" -i "$WG_IF" -o "$TUN_IF" -j ACCEPT
}

# Function gate_close removes that permission. Anything in flight is dropped.
gate_close() {
  ipt_remove "$FWD_CHAIN" -i "$WG_IF" -o "$TUN_IF" -j ACCEPT
}

# Function gate_state prints "open" or "closed".
gate_state() {
  if iptables -C "$FWD_CHAIN" -i "$WG_IF" -o "$TUN_IF" -j ACCEPT 2>/dev/null
  then
    echo 'open'
  else
    echo 'closed'
  fi
}

case "${1:-}" in
'up')
  check_forwarding

  # Best effort only, and for the same read-only reason usually a no-op. IPv6 is
  # not a leak path regardless: the container has no IPv6 address on the default
  # docker bridge, and the peer configurations deliberately omit ::/0, so no IPv6
  # traffic can enter or leave the tunnel in the first place.
  sysctl -q -w net.ipv6.conf.all.disable_ipv6=1 2>/dev/null || true
  sysctl -q -w net.ipv6.conf.default.disable_ipv6=1 2>/dev/null || true

  setup_tun
  setup_routing
  setup_firewall
  # Start closed. The watchdog opens the gate once it has proven that traffic
  # actually reaches the internet through AdGuard.
  gate_close
  log "up (kill switch armed, gate closed)"
  ;;

'down')
  gate_close
  ip rule del from "$WG_SUBNET" lookup "$RT_TABLE" 2>/dev/null || true
  ip route flush table "$RT_TABLE" 2>/dev/null || true
  iptables -t nat -D POSTROUTING -s "$WG_SUBNET" -o "$TUN_IF" -j MASQUERADE 2>/dev/null || true
  ipt_remove FORWARD -j "$FWD_CHAIN"
  iptables -F "$FWD_CHAIN" 2>/dev/null || true
  iptables -X "$FWD_CHAIN" 2>/dev/null || true
  ip link del "$TUN_IF" 2>/dev/null || true
  log 'down'
  ;;

'gate')
  case "${2:-state}" in
    'open')  gate_open ;;
    'close') gate_close ;;
    'state') gate_state ;;
    *) echo "usage: $0 gate open|close|state" >&2; exit 2 ;;
  esac
  ;;

'status')
  # Infrastructure health only. The state of the AdGuard link is reported in
  # the panel, not here: a container that is merely waiting for you to log in
  # is not broken.
  rc=0
  ip link show "$WG_IF" >/dev/null 2>&1 || { echo "wg0 missing"; rc=1; }
  ip link show "$TUN_IF" >/dev/null 2>&1 || { echo "tun0 missing"; rc=1; }
  ip rule show | grep -q "lookup ${RT_TABLE}" || { echo "policy route missing"; rc=1; }
  iptables -C FORWARD -j "$FWD_CHAIN" 2>/dev/null || { echo "kill switch missing"; rc=1; }
  [ "$(iptables -S FORWARD | head -1)" = '-P FORWARD DROP' ] || { echo "FORWARD policy is not DROP"; rc=1; }
  if [ "$rc" -eq 0 ]
  then
    echo "ok (gate: $(gate_state))"
  fi
  exit "$rc"
  ;;

*)
  echo "usage: $0 up|down|status|gate open|close|state" >&2
  exit 2
  ;;
esac
