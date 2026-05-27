"""Deployment checks for the qdistro xdg-desktop-portal backend."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_portal_backend_has_dbus_activation_service():
    service = (
        ROOT
        / "deploy/dbus-1/services/org.freedesktop.impl.portal.qdistro.service"
    )
    text = service.read_text(encoding="utf-8")
    assert "Name=org.freedesktop.impl.portal.qdistro" in text
    assert "SystemdService=qdistro-portal-backend.service" in text


def test_portal_descriptor_does_not_advertise_unimplemented_screenshot():
    portal = ROOT / "deploy/portals/qdistro.portal"
    text = portal.read_text(encoding="utf-8")
    assert "org.freedesktop.impl.portal.FileChooser" in text
    assert "org.freedesktop.impl.portal.Screenshot" not in text
