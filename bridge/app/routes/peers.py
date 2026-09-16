"""WireGuard peer management: create, edit, download, QR."""

from __future__ import annotations

import io

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse, Response

from app import events, wireguard
from app.config import SETTINGS
from app.web import apply_wireguard_changes, endpoint_or_hint, redirect, render

router = APIRouter()


def _svg_qr(payload: str) -> bytes:
    """Render a QR code as SVG - no Pillow, no binary image handling."""
    image = qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue()


@router.get("/peers")
def peers_page(request: Request):
    endpoint, endpoint_warning = endpoint_or_hint()
    return render(
        request, "peers.html",
        peers=wireguard.list_peers(),
        endpoint=endpoint,
        endpoint_warning=endpoint_warning,
        suggested_name="UniFi Gateway" if not wireguard.list_peers() else "",
    )


@router.post("/peers")
def create_peer(name: str = Form(...), behind_networks: str = Form("")):
    try:
        peer = wireguard.create_peer(name, behind_networks)
    except wireguard.PeerError as exc:
        return redirect("/peers", err=str(exc))

    error = apply_wireguard_changes()
    if error:
        return redirect("/peers", err=f"Peer angelegt, aber nicht aktiviert: {error}")
    return redirect(f"/peers?created={peer.id}", msg=f"Peer '{peer.name}' angelegt.")


@router.post("/peers/{peer_id}/update")
def update_peer(peer_id: int, name: str = Form(...), behind_networks: str = Form("")):
    try:
        wireguard.update_peer(peer_id, name=name, behind_networks=behind_networks)
    except wireguard.PeerError as exc:
        return redirect("/peers", err=str(exc))
    error = apply_wireguard_changes()
    return redirect("/peers", msg="Peer gespeichert." if not error else "", err=error)


@router.post("/peers/{peer_id}/toggle")
def toggle_peer(peer_id: int):
    peer = wireguard.get_peer(peer_id)
    if peer is None:
        return redirect("/peers", err="Dieser Peer existiert nicht mehr.")
    try:
        wireguard.update_peer(peer_id, enabled=not peer.enabled)
    except wireguard.PeerError as exc:
        return redirect("/peers", err=str(exc))
    error = apply_wireguard_changes()
    action = "deaktiviert" if peer.enabled else "aktiviert"
    return redirect("/peers", msg=f"Peer '{peer.name}' {action}." if not error else "", err=error)


@router.post("/peers/{peer_id}/delete")
def delete_peer(peer_id: int):
    wireguard.delete_peer(peer_id)
    error = apply_wireguard_changes()
    return redirect("/peers", msg="Peer gelöscht." if not error else "", err=error)


@router.get("/peers/{peer_id}/config")
def download_config(peer_id: int):
    peer = wireguard.get_peer(peer_id)
    if peer is None:
        return PlainTextResponse("Peer nicht gefunden", status_code=404)
    events.record(events.PEER, f"Konfiguration für '{peer.name}' heruntergeladen")
    return PlainTextResponse(
        wireguard.render_peer_config(peer, SETTINGS.wg_endpoint),
        headers={"Content-Disposition": f'attachment; filename="{peer.filename}"'},
    )


@router.get("/peers/{peer_id}/qr")
def peer_qr(peer_id: int):
    peer = wireguard.get_peer(peer_id)
    if peer is None:
        return PlainTextResponse("Peer nicht gefunden", status_code=404)
    payload = wireguard.render_peer_config(peer, SETTINGS.wg_endpoint)
    return Response(_svg_qr(payload), media_type="image/svg+xml")
