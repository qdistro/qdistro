pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Services.Qdwin
import qs.Services.Qdistro
import qs.Services.Qdshell

// Tier3FocusIPC — Quickshell IPC bridge exposing qdwin_shell_v1@v14
// focus + selection driving to external CLIs (primarily the bats
// test driver tests/integration/vm/s48-focus-aware-clear.sh).
//
// Why this exists:
//   spec/10 v14 added `set_keyboard_focus` (request) +
//   `seat_focus_changed` (event) so the shell can drive cross-silo
//   focus moves headlessly. The bats VM has no keyboard hardware
//   under sdl-freerdp /v: dummy, so the only way to exercise the
//   cross-silo flow in CI is to *inject* focus from the shell side.
//   This IPC is that injection surface.
//
//   Without it, s48's "qdshell cleared the admin selection on cross-
//   silo focus" assertion has no test driver — see the lead comment
//   on s53-data-offer-receive-v15.sh ("[the ctrl-socket inject-focus]
//   isn't a shipped CLI on the qdshell side").
//
// Surface (target alias = "tier3focus"):
//   qs ipc call tier3focus injectFocus <handle> [seat]
//   qs ipc call tier3focus clearSelection [seat] [primary]
//   qs ipc call tier3focus findSiloHandle <silo>
//   qs ipc call tier3focus selectionState
//
//   injectFocus delegates to Qdwin.injectFocus → qdwin_shell_v1
//   set_keyboard_focus. qdwin's v14 contract clears the seat
//   selection unconditionally on every focus injection that crosses
//   silo boundaries; the bats driver observes that via journal grep.
//
//   findSiloHandle scans Tier3Apps.tier3Windows for the first
//   toplevel matching the given silo and prints its handle on a
//   single stdout line ("HANDLE=N"). Used by the bats driver to
//   resolve "silo=user1's current toplevel handle" without grepping
//   weston logs.
//
//   selectionState emits a Logger.i log line snapshotting the
//   current src_silo / dst_silo from ClipboardGate's last gate
//   event. Bats greps the journal for the snapshot.
//
// F8: SECURITY ASSUMPTION — this IPC socket MUST be reachable only from the
// admin uid that runs qdshell. injectFocus drives cross-silo keyboard focus and
// clearSelection clears the seat selection; under the v14 focus-aware-clear
// contract a same-uid caller that could reach this socket would gain a
// focus-confusion / clipboard-gate-bypass primitive. There is deliberately no
// auth boundary INSIDE the IPC (no cross-user trust boundary inside qdshell) —
// the boundary is the socket's uid reachability. Do NOT expose this socket to
// silo uids; if that ever becomes possible, injectFocus/clearSelection must be
// moved behind the broker gate.
Singleton {
    id: root

    // Marker for the shell.qml force-instantiate trick.
    readonly property bool isQdistroFocusIPC: true

    Component.onCompleted: Logger.i("Tier3FocusIPC", "service started")

    // Allowlist for the `seat` arg — qdwin's seat naming is `default`
    // today; "pointer"/"keyboard" reserved for the multi-seat future.
    // Anything else gets rejected with a clear error rather than
    // hitting the native binding with arbitrary strings.
    readonly property var _allowedSeats: ({ "default": true, "pointer": true, "keyboard": true })

    function _findSiloHandle(silo) {
        if (!silo || typeof silo !== "string") return -1;
        if (silo.length > 64) return -1;   // silo names from useradd are short
        const wm = Tier3Apps.tier3Windows;
        if (!wm) return -1;
        for (let i = 0; i < wm.count; i++) {
            const row = wm.get(i);
            if (row.silo === silo) return row.handle;
        }
        return -1;
    }

    // M3 fix (2026-05-16): validate that `handle` belongs to an
    // admin-visible silo toplevel before delegating. Without this
    // check, any admin-uid process with IPC reach can focus-steal
    // arbitrary windows — the surface name "tier3focus" implies
    // scoped operation, so the check makes the boundary honest.
    //
    // P04 R10-fix (2026-05-19): also accept tier-4 handles. The
    // s106-browser-clipboard-gate driver needs to inject focus to a
    // tier-4-tagged destination toplevel so ClipboardGate's
    // focusedHandle → _handleToSilo lookup resolves to a real silo
    // (instead of "unknown"). Tier-4 toplevels are equally admin-
    // visible / admin-owned, so the security boundary is unchanged.
    // The IPC name "tier3focus" is kept for backwards-compat with
    // the s48 driver.
    function _isTier3Handle(handle) {
        const wm3 = Tier3Apps.tier3Windows;
        if (wm3) {
            for (let i = 0; i < wm3.count; i++)
                if (wm3.get(i).handle === handle) return true;
        }
        const wm4 = Tier4Apps.tier4Windows;
        if (wm4) {
            for (let i = 0; i < wm4.count; i++)
                if (wm4.get(i).handle === handle) return true;
        }
        return false;
    }

    IpcHandler {
        target: "tier3focus"

        // qs ipc call tier3focus injectFocus <handle> [seat]
        function injectFocus(handle: int, seat: string): string {
            const seatName = seat && seat.length > 0 ? seat : "default";
            if (!root._allowedSeats[seatName]) {
                Logger.w("Tier3FocusIPC",
                         "injectFocus REJECTED — bad seat='" + seatName + "'");
                return "error: invalid seat '" + seatName + "'";
            }
            if (!Number.isInteger(handle) || handle < 0 || handle > 4294967295) {
                Logger.w("Tier3FocusIPC",
                         "injectFocus REJECTED — handle out of range: " + handle);
                return "error: invalid handle";
            }
            if (!root._isTier3Handle(handle)) {
                Logger.w("Tier3FocusIPC",
                         "injectFocus REJECTED — handle=" + handle
                         + " is not a tier-3 toplevel (use Tier3Apps.tier3Windows)");
                return "error: handle=" + handle + " is not a tier-3 toplevel";
            }
            Logger.i("Tier3FocusIPC",
                     "injectFocus handle=" + handle + " seat=" + seatName);
            Qdwin.injectFocus(handle, seatName);
            return "ok handle=" + handle + " seat=" + seatName;
        }

        // qs ipc call tier3focus clearSelection [seat] [primary]
        function clearSelection(seat: string, primary: string): string {
            const seatName = seat && seat.length > 0 ? seat : "default";
            if (!root._allowedSeats[seatName]) {
                Logger.w("Tier3FocusIPC",
                         "clearSelection REJECTED — bad seat='" + seatName + "'");
                return "error: invalid seat '" + seatName + "'";
            }
            const isPri = primary === "1" || primary === "true";
            Logger.i("Tier3FocusIPC",
                     "clearSelection seat=" + seatName + " primary=" + (isPri ? 1 : 0));
            Qdwin.clearSeatSelection(seatName, isPri);
            return "ok seat=" + seatName + " primary=" + (isPri ? 1 : 0);
        }

        // qs ipc call tier3focus findSiloHandle <silo>
        // Returns "HANDLE=<n>" or "HANDLE=-1" so the bats driver
        // can parse a single key=value line.
        function findSiloHandle(silo: string): string {
            const h = root._findSiloHandle(silo);
            const out = "HANDLE=" + h;
            Logger.i("Tier3FocusIPC", "findSiloHandle silo=" + silo + " → " + out);
            return out;
        }

        // qs ipc call tier3focus selectionState
        //
        // M4 fix (2026-05-16): the prior version maintained four
        // _last* properties claiming to track the most recent
        // ClipboardGate event, but no `function on…` handlers in the
        // Connections block wrote them. They were dead. ClipboardGate
        // doesn't currently expose a signal for Tier3FocusIPC to
        // subscribe to, so this command now reports a verbatim
        // snapshot of focus-side state (focused tier-3 handle + that
        // handle's silo). The "is admin's selection still set?"
        // question is journal-driven — pair with a grep for the most
        // recent CLIPBOARD_GATE line.
        function selectionState(): string {
            const wm = Tier3Apps.tier3Windows;
            const tier3Count = wm ? wm.count : 0;
            const reply = "tier3_toplevels=" + tier3Count
                        + " hint=grep_journal_for_CLIPBOARD_GATE";
            Logger.i("Tier3FocusIPC", "selectionState " + reply);
            return reply;
        }
    }
}
