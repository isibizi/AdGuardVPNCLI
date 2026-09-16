"""Peer addressing, validation and configuration rendering."""

from __future__ import annotations

import pytest

from app import wireguard


def test_addresses_are_allocated_in_order_and_skip_the_bridge():
    first = wireguard.create_peer("UniFi Gateway")
    second = wireguard.create_peer("Handy")
    assert first.address == "10.8.0.2"   # .1 belongs to the bridge itself
    assert second.address == "10.8.0.3"


def test_deleted_addresses_are_reused():
    first = wireguard.create_peer("A")
    wireguard.create_peer("B")
    wireguard.delete_peer(first.id)
    assert wireguard.create_peer("C").address == "10.8.0.2"


def test_duplicate_names_are_rejected_with_a_readable_message():
    wireguard.create_peer("Laptop")
    with pytest.raises(wireguard.PeerError, match="existiert bereits"):
        wireguard.create_peer("Laptop")


@pytest.mark.parametrize("value", ["", "   ", "a" * 42, "bad/name"])
def test_invalid_names_are_rejected(value):
    with pytest.raises(wireguard.PeerError):
        wireguard.create_peer(value)


def test_behind_networks_are_normalised():
    peer = wireguard.create_peer("UniFi", "192.168.1.5/24, 172.16.0.0/16")
    assert peer.networks == ["192.168.1.0/24", "172.16.0.0/16"]


def test_network_overlapping_the_tunnel_is_rejected():
    with pytest.raises(wireguard.PeerError, match="überschneidet"):
        wireguard.create_peer("UniFi", "10.8.0.0/25")


def test_invalid_network_is_rejected():
    with pytest.raises(wireguard.PeerError, match="gültiges Netz"):
        wireguard.create_peer("UniFi", "nonsense")


def test_peer_config_is_unifi_compatible():
    peer = wireguard.create_peer("UniFi Gateway", "192.168.1.0/24")
    config = wireguard.render_peer_config(peer)

    assert "[Interface]" in config and "[Peer]" in config
    assert f"PrivateKey = {peer.private_key}" in config
    assert "Address = 10.8.0.2/32" in config
    assert "DNS = 94.140.14.14, 94.140.15.15" in config
    assert "MTU = 1380" in config
    assert "AllowedIPs = 0.0.0.0/0" in config
    assert "Endpoint = vpn.example.org:51820" in config
    assert "PersistentKeepalive = 25" in config
    # IPv6 must not be claimed: the bridge does not carry it.
    assert "::/0" not in config


def test_server_config_lists_enabled_peers_only():
    enabled = wireguard.create_peer("Aktiv")
    disabled = wireguard.create_peer("Inaktiv")
    wireguard.update_peer(disabled.id, enabled=False)

    config = wireguard.render_server_config()
    assert enabled.public_key in config
    assert disabled.public_key not in config
    assert "ListenPort = 51820" in config
    # Keys wg setconf does not understand must not leak into the server config.
    assert "Address" not in config and "DNS" not in config and "MTU" not in config


def test_routes_cover_networks_behind_enabled_peers():
    wireguard.create_peer("UniFi", "192.168.1.0/24")
    off = wireguard.create_peer("Alt", "192.168.9.0/24")
    wireguard.update_peer(off.id, enabled=False)
    assert wireguard.collect_routes() == ["192.168.1.0/24"]


def test_parse_wg_dump():
    dump = (
        "privkey\tpubkey\t51820\toff\n"
        "PEERKEY=\tPSK=\t203.0.113.7:12345\t10.8.0.2/32\t1700000000\t1024\t2048\t25\n"
        "OTHER=\t(none)\t(none)\t10.8.0.3/32\t0\t0\t0\toff\n"
    )
    parsed = wireguard.parse_wg_dump(dump)
    assert parsed["PEERKEY="]["endpoint"] == "203.0.113.7:12345"
    assert parsed["PEERKEY="]["rx"] == 1024
    assert parsed["PEERKEY="]["tx"] == 2048
    assert parsed["PEERKEY="]["last_handshake"] == 1700000000
    assert parsed["OTHER="]["endpoint"] == ""
