"""Secret source backed by the qdistro pwd/vault daemon.

Adapts a workflow ``needs:`` item path (e.g. ``vault/dev/github-ssh-key``)
to the daemon's ``DeliverToWorkflow(vault, tag, run_id)`` D-Bus method
and returns the unsealed bytes. The engine wraps the result in a
wipeable ``SecretValue`` and hands it to a delivery mechanism; this
module's only job is the fetch.

The secret transits the private system-bus reply only — it is never
written to disk here or logged. dbus is imported lazily so the workflow
package still imports in environments without it.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("qdistro.workflow.secret_source")

PWD_BUS_NAME = "org.qdistro.Pwd1"
PWD_OBJ_PATH = "/org/qdistro/Pwd1"


def parse_item(item: str) -> tuple[str, str]:
    """Split a ``needs:`` item path into (vault, tag).

    ``vault/dev/github-ssh-key`` -> ("dev", "github-ssh-key").
    A leading ``vault/`` is optional. The tag keeps any further
    slashes (e.g. ``vault/dev/portal/app`` -> ("dev", "portal/app")).
    """
    if not item or not isinstance(item, str):
        raise ValueError("secret item must be a non-empty string")
    path = item.strip().strip("/")
    if path.startswith("vault/"):
        path = path[len("vault/"):]
    parts = path.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"item {item!r} must be 'vault/<name>/<tag>' (got {path!r})")
    return parts[0], parts[1]


class PwdSecretSource:
    """Fetches secrets from the pwd daemon over the system bus."""

    def __init__(self, bus=None, bus_name: str = PWD_BUS_NAME,
                 obj_path: str = PWD_OBJ_PATH):
        self._bus = bus
        self._bus_name = bus_name
        self._obj_path = obj_path

    def _interface(self):
        import dbus  # lazy
        bus = self._bus
        if bus is None:
            bus = dbus.SystemBus()
            self._bus = bus
        proxy = bus.get_object(self._bus_name, self._obj_path)
        return dbus.Interface(proxy, self._bus_name)

    def fetch(self, item: str, run_id: str = "") -> bytes:
        vault, tag = parse_item(item)
        iface = self._interface()
        payload = iface.DeliverToWorkflow(vault, tag, run_id)
        return str(payload).encode("utf-8")
