"""Best-effort pwd vault lifecycle notifications from qdlocker."""

from __future__ import annotations

import asyncio
import logging
import threading

log = logging.getLogger("qdlocker.pwd_lifecycle")

PWD_BUS = "org.qdistro.Pwd1"
PWD_OBJ = "/org/qdistro/Pwd1"
PWD_IFACE = "org.qdistro.Pwd1"


class PwdLifecycleNotifier:
    """Notify qdistro-pwd when the compositor is entering lock state.

    The screen lock must not depend on pwd being reachable. Calls run in a
    daemon thread and are intentionally best-effort: failures are logged and the
    lock continues.
    """

    def notify_screen_lock(self, reason: str) -> None:
        safe_reason = (
            reason if reason in {"idle", "lid", "suspend", "manual"}
            else "manual"
        )
        thread = threading.Thread(
            target=self._run_notify,
            args=(f"screen-lock:{safe_reason}",),
            name="qdlocker-pwd-relock",
            daemon=True,
        )
        thread.start()

    def _run_notify(self, reason: str) -> None:
        try:
            asyncio.run(self._call_lock_all(reason))
        except Exception:
            log.exception("pwd lifecycle relock failed")

    async def _call_lock_all(self, reason: str) -> None:
        try:
            from dbus_next import BusType
            from dbus_next.aio import MessageBus
        except ImportError:
            log.warning("dbus-next not installed; pwd lifecycle relock unavailable")
            return

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            intro = await asyncio.wait_for(
                bus.introspect(PWD_BUS, PWD_OBJ), timeout=2.0)
            obj = bus.get_proxy_object(PWD_BUS, PWD_OBJ, intro)
            iface = obj.get_interface(PWD_IFACE)
            await asyncio.wait_for(iface.call_lock_all_vaults(reason), timeout=2.0)
        finally:
            try:
                await asyncio.wait_for(bus.disconnect(), timeout=1.0)
            except Exception:
                pass
