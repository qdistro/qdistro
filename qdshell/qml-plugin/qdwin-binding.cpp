// qdwin-binding.cpp — see qdwin-binding.h for shape + rationale.
//
// Listener stubs we don't yet expose to QML still need to exist
// (the qdwin_shell_v1_listener struct is checked field-by-field at
// add_listener time and a NULL slot crashes on dispatch). For each
// field we either forward to a Q_SIGNAL or hold a no-op so future
// phase-2 wiring is one edit.
//
// SPDX-License-Identifier: GPL-3.0-or-later

#include "qdwin-binding.h"
#include "ctrl-server.h"

#include <wayland-client.h>
#include "qdwin-shell-v1-client-protocol.h"
#include "weston-output-capture-client-protocol.h"
#include "ext-workspace-v1-client-protocol.h"
#include "wlr-output-management-unstable-v1-client-protocol.h"
#include "ext-idle-notify-v1-client-protocol.h"

#include <algorithm>
#include <initializer_list>

#include <QDebug>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QImage>
#include <QMetaType>
#include <QProcess>
#include <QRegularExpression>
#include <QString>
#include <QStringList>
#include <QTemporaryFile>
#include <QVariantMap>

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <limits>
#include <poll.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

namespace {
// Bump to 23 to pick up `selection_set_source_identity` — the v23 sidecar
// emitted IMMEDIATELY BEFORE `selection_set` carrying the secctx tuple
// (engine, app_id, instance_id) of the wl_client that issued the
// set_selection. ClipboardGate.qml uses it to derive src_silo from the
// wire instead of from keyboard-focus state, closing the R9 P04 hole
// where a tagged wl_client without focused-toplevel ownership had its
// src_silo collapse to the focused admin shell's silo.
//
// Earlier bumps in this file:
//   22 — toplevel_peer_identity (Option-B identity sidecar, see
//        todo/decisions/secctx-identity-contract.md)
// Bump to 24 to pick up `toplevel_workspace` (per-window→workspace
// sidecar for the bar's occupancy) and the `move_toplevel_to_workspace`
// request. The workspace list/active state itself rides the standard
// ext-workspace-v1 client below, not this private binding. See
// todo/decisions/qdwin-workspaces-ext-protocol.md.
// Bump to 25 for live window-manager policy (`set_wm_policy`,
// `request_fullscreen`, `request_tile`) — the WindowManager settings tab's
// live-apply path — plus the previously-unwired v19 `register_hotkey` /
// `hotkey_pressed` (WM keyboard shortcuts).
// Bump to 26 for set_display_power (idle/DPMS — the Power tab's display-off
// timer). The idle *trigger* rides the standard ext-idle-notify-v1 client
// bound below, not this private binding.
// Bump to 27 for set_workspace_name (ext-workspace-v1 NAME parity — push the
// user's custom workspace names so qdwin echoes them on the standard
// ext_workspace_handle_v1.name event to every bar).
// Bump to 28 for live input config: set_pointer_config (the Mouse tab's
// libinput pointer/touchpad policy) and set_key_repeat (the Keyboard tab's
// xkb repeat rate/delay). Before v28 those tabs were persist-only.
// Versions 31–33 carry mainline app-id updates and framebuffer capture.
// Version 34 appends remote display identity/input/drain without changing
// the already shipped request and event opcodes.
constexpr uint32_t kBindVersion = 34;
constexpr int kCaptureTimeoutMs = 8000;
constexpr int kBrokerStartTimeoutMs = 250;
constexpr int kBrokerGateTimeoutMs = 2000;
constexpr int kBrokerDefaultTimeoutMs = 200;
constexpr auto kBrokerGateBusctlTimeout = "--timeout=2s";
constexpr auto kBrokerDefaultBusctlTimeout = "--timeout=200ms";

inline QString qstr(const char *s) {
    return s ? QString::fromUtf8(s) : QString();
}

constexpr uint32_t fourcc(char a, char b, char c, char d) {
    return static_cast<uint32_t>(a) |
           (static_cast<uint32_t>(b) << 8) |
           (static_cast<uint32_t>(c) << 16) |
           (static_cast<uint32_t>(d) << 24);
}

constexpr uint32_t kDrmArgb8888 = fourcc('A', 'R', '2', '4');
constexpr uint32_t kDrmXrgb8888 = fourcc('X', 'R', '2', '4');

// F5: wire-sourced identity strings (app_id, instance_id, selinux label, exe,
// sandbox engine, mime types) are relayed from possibly-malicious silo clients
// and then handed to busctl as argv and across C ABIs via toUtf8().constData().
// A QString may carry an embedded NUL or other C0 control char; the C-string
// conversion truncates at the first NUL, so the broker would key its policy on a
// DIFFERENT (shorter) identity than the QML gate keyed on — a cross-identity
// policy desync. Reject such strings FAIL-CLOSED before they reach a security
// decision; do NOT strip, because stripping rewrites identity and can manufacture
// collisions.
inline bool hasControlChars(const QString &s) {
    for (const QChar c : s) {
        const ushort u = c.unicode();
        if (u < 0x20 || u == 0x7F)
            return true;
    }
    return false;
}

inline bool anyControlChars(std::initializer_list<const QString *> strs) {
    for (const QString *s : strs)
        if (hasControlChars(*s))
            return true;
    return false;
}

// Fail-closed result for a broker-gate call whose identity inputs were rejected.
// Mirrors the error/timeout edges the QML BrokerGate already treats as "deny".
inline QVariantMap rejectedIdentityResult() {
    return {
        {QStringLiteral("exitCode"), -1},
        {QStringLiteral("stdout"), QString()},
        {QStringLiteral("stderr"),
         QStringLiteral("rejected: control character in identity string")},
        {QStringLiteral("timedOut"), false},
    };
}

// Outbound informational strings (decision reasons, seat names) are not identity
// decision inputs, so here stripping control chars is the safe move rather than
// failing the action.
inline QString stripControlChars(const QString &s) {
    QString out;
    out.reserve(s.size());
    for (const QChar c : s) {
        const ushort u = c.unicode();
        if (u >= 0x20 && u != 0x7F)
            out.append(c);
    }
    return out;
}

void appendVariantDict(QStringList &args, const QVariantMap &details) {
    args.append(QString::number(details.size()));
    for (auto it = details.cbegin(); it != details.cend(); ++it) {
        args.append(it.key());
        const QVariant value = it.value();
        const int typeId = value.metaType().id();
        if (it.key() == QStringLiteral("origin_uid")) {
            args.append(QStringLiteral("u"));
            args.append(QString::number(value.toUInt()));
            continue;
        }
        switch (typeId) {
        case QMetaType::Bool:
            args.append(QStringLiteral("b"));
            args.append(value.toBool() ? QStringLiteral("true")
                                       : QStringLiteral("false"));
            break;
        case QMetaType::Int:
        case QMetaType::LongLong:
            args.append(QStringLiteral("x"));
            args.append(QString::number(value.toLongLong()));
            break;
        case QMetaType::UInt:
        case QMetaType::ULongLong:
            args.append(QStringLiteral("t"));
            args.append(QString::number(value.toULongLong()));
            break;
        case QMetaType::Double:
            if (value.toDouble() >= 0) {
                args.append(QStringLiteral("t"));
                args.append(QString::number(static_cast<qulonglong>(value.toDouble())));
            } else {
                args.append(QStringLiteral("x"));
                args.append(QString::number(static_cast<qlonglong>(value.toDouble())));
            }
            break;
        default:
            args.append(QStringLiteral("s"));
            args.append(value.toString());
            break;
        }
    }
}
}

// -------------------- C wayland listener trampolines ---------------------

// All trampolines forward to QdwinBinding via the void *data pointer
// that we register with wl_registry_add_listener / qdwin_shell_v1_add_listener.

struct QdwinBindingDispatch {
    static void hello(void *d, qdwin_shell_v1 *, uint32_t uid) {
        auto *b = static_cast<QdwinBinding *>(d);
        b->setBound(true);
        emit b->hello(uid);
    }
    static void toplevel_added(void *d, qdwin_shell_v1 *,
                               uint32_t handle, uint32_t owner_uid,
                               const char *app_id, const char *title,
                               uint32_t is_xwayland) {
        auto *b = static_cast<QdwinBinding *>(d);
        emit b->toplevelAdded(handle, owner_uid, qstr(app_id), qstr(title),
                              is_xwayland != 0);
    }
    static void toplevel_geometry(void *d, qdwin_shell_v1 *,
                                  uint32_t handle, int32_t x, int32_t y,
                                  uint32_t w, uint32_t h) {
        auto *b = static_cast<QdwinBinding *>(d);
        emit b->toplevelGeometry(handle, x, y, w, h);
    }
    static void toplevel_state(void *d, qdwin_shell_v1 *,
                               uint32_t handle, uint32_t state) {
        auto *b = static_cast<QdwinBinding *>(d);
        emit b->toplevelState(handle, state);
    }
    static void toplevel_title(void *d, qdwin_shell_v1 *,
                               uint32_t handle, const char *title) {
        auto *b = static_cast<QdwinBinding *>(d);
        emit b->toplevelTitle(handle, qstr(title));
    }
    static void toplevel_app_id(void *d, qdwin_shell_v1 *,
                                uint32_t handle, const char *app_id) {
        auto *b = static_cast<QdwinBinding *>(d);
        emit b->toplevelAppId(handle, qstr(app_id));
    }
    static void capture_served_stale(void *d, qdwin_shell_v1 *,
                                     const char *output_name, uint32_t age_ms,
                                     uint32_t msc) {
        auto *b = static_cast<QdwinBinding *>(d);
        b->captureStaleServed_ = true;
        b->captureStaleAgeMs_ = age_ms;
        b->captureStaleMsc_ = msc;
        qWarning().noquote()
            << "qdwin-binding: capture served STALE retained frame output="
            << qstr(output_name) << "age_ms=" << age_ms << "msc=" << msc;
    }
    static void toplevel_removed(void *d, qdwin_shell_v1 *, uint32_t handle) {
        auto *b = static_cast<QdwinBinding *>(d);
        emit b->toplevelRemoved(handle);
    }
    static void locked_changed(void *, qdwin_shell_v1 *, uint32_t) {}
    static void seat_created(void *, qdwin_shell_v1 *, const char *) {}
    static void seat_removed(void *, qdwin_shell_v1 *, const char *) {}
    static void output_created(void *, qdwin_shell_v1 *, const char *) {}
    static void output_removed(void *, qdwin_shell_v1 *, const char *) {}
    static void launcher_requested(void *d, qdwin_shell_v1 *) {
        emit static_cast<QdwinBinding *>(d)->launcherRequested();
    }
    static void switcher_next(void *d, qdwin_shell_v1 *, int32_t dir) {
        emit static_cast<QdwinBinding *>(d)->switcherNext(dir);
    }
    static void switcher_commit(void *d, qdwin_shell_v1 *) {
        emit static_cast<QdwinBinding *>(d)->switcherCommit();
    }
    static void lock_requested(void *d, qdwin_shell_v1 *) {
        emit static_cast<QdwinBinding *>(d)->lockRequested();
    }
    static void idle_lock_hint(void *d, qdwin_shell_v1 *, uint32_t reason) {
        emit static_cast<QdwinBinding *>(d)->idleLockHint(reason);
    }
    static void nested_proxy_pending(void *d, qdwin_shell_v1 *,
                                     uint32_t handle, const char *app_id,
                                     uint32_t origin_uid) {
        auto *b = static_cast<QdwinBinding *>(d);
        emit b->nestedProxyPending(handle, qstr(app_id), origin_uid);
    }
    static void nested_proxy_pixel_source(void *d, qdwin_shell_v1 *,
                                          uint32_t handle,
                                          const char *pw_node,
                                          const char *input_sink) {
        auto *b = static_cast<QdwinBinding *>(d);
        emit b->nestedProxyPixelSource(handle,
                                       qstr(pw_node), qstr(input_sink));
    }
    static void nested_proxy_remote_identity(
            void *d, qdwin_shell_v1 *, uint32_t handle,
            const char *source_machine, const char *trust_domain_id,
            const char *stream_id, uint32_t generation_hi,
            uint32_t generation_lo) {
        auto *b = static_cast<QdwinBinding *>(d);
        const quint64 generation = (quint64(generation_hi) << 32)
                                   | quint64(generation_lo);
        emit b->nestedProxyRemoteIdentity(
            handle, qstr(source_machine), qstr(trust_domain_id),
            qstr(stream_id), generation);
    }
    static void remote_output_input_result(
            void *d, qdwin_shell_v1 *, const char *output_name,
            uint32_t enabled, uint32_t applied) {
        auto *b = static_cast<QdwinBinding *>(d);
        emit b->remoteOutputInputResult(
            qstr(output_name), enabled != 0, applied != 0);
    }
    static void remote_output_drain_result(
            void *d, qdwin_shell_v1 *, const char *output_name,
            uint32_t applied) {
        auto *b = static_cast<QdwinBinding *>(d);
        emit b->remoteOutputDrainResult(qstr(output_name), applied != 0);
    }
    // spec/10 selection_set — forward to QML so ClipboardGate can
    // consult the broker and call clearSelection on a deny verdict.
    static void selection_set(void *d, qdwin_shell_v1 *,
                              const char *seat_name, uint32_t source_handle,
                              const char *mime_types_concat,
                              uint32_t is_primary) {
        auto *b = static_cast<QdwinBinding *>(d);
        emit b->selectionSet(qstr(seat_name), source_handle,
                             qstr(mime_types_concat), is_primary);
    }
    // v23 sidecar — qdwin_shell_v1.selection_set_source_identity fires
    // IMMEDIATELY BEFORE the matching `selection_set` for tagged source
    // clients. We forward the tuple as a distinct signal; ClipboardGate
    // stashes it as "pending" and consumes it on the very next
    // selectionSet. Order is preserved because wayland dispatch is
    // single-threaded and Qt direct-connect signal delivery runs
    // synchronously inside this dispatch frame.
    static void selection_set_source_identity(void *d, qdwin_shell_v1 *,
                                              const char *src_sandbox_engine,
                                              const char *src_app_id,
                                              const char *src_instance_id) {
        auto *b = static_cast<QdwinBinding *>(d);
        emit b->selectionSetSourceIdentity(qstr(src_sandbox_engine),
                                           qstr(src_app_id),
                                           qstr(src_instance_id));
    }
    static void activation_pending(void *d, qdwin_shell_v1 *,
                                   uint32_t handle, uint32_t source_handle,
                                   uint32_t target_handle,
                                   const char *source_app_id) {
        auto *b = static_cast<QdwinBinding *>(d);
        emit b->activationPending(handle, source_handle, target_handle,
                                  qstr(source_app_id));
    }
    // wp_security_context_v1 tag — load-bearing for both the cold-
    // start placeholder resolution (claude/tier2-podman) and spec/10's
    // handle→silo map for the clipboard gate.
    static void toplevel_security_context(void *d, qdwin_shell_v1 *,
                                          uint32_t handle,
                                          const char *sandbox_engine,
                                          const char *app_id,
                                          const char *instance_id) {
        auto *b = static_cast<QdwinBinding *>(d);
        emit b->toplevelSecurityContext(handle, qstr(sandbox_engine),
                                        qstr(app_id), qstr(instance_id));
    }
    // Option-B identity sidecar (qdwin_shell_v1@v22). Fires immediately
    // after `toplevel_security_context` for the same handle. starttime
    // is reassembled from the lo/hi uint32 split that the protocol
    // carries (wayland has no native uint64 arg type).
    static void toplevel_peer_identity(void *d, qdwin_shell_v1 *,
                                       uint32_t handle,
                                       uint32_t peer_pid,
                                       uint32_t peer_starttime_lo,
                                       uint32_t peer_starttime_hi,
                                       uint32_t peer_uid,
                                       const char *peer_exe,
                                       const char *peer_selinux_label) {
        auto *b = static_cast<QdwinBinding *>(d);
        quint64 st = (static_cast<quint64>(peer_starttime_hi) << 32)
                     | static_cast<quint64>(peer_starttime_lo);
        emit b->toplevelPeerIdentity(handle, peer_pid, st, peer_uid,
                                     qstr(peer_exe),
                                     qstr(peer_selinux_label));
    }
    static void seat_focus_changed(void *d, qdwin_shell_v1 *,
                                   const char *seat_name,
                                   uint32_t focused_handle) {
        auto *b = static_cast<QdwinBinding *>(d);
        b->setFocused(qstr(seat_name), focused_handle);
        emit b->seatFocusChanged(qstr(seat_name), focused_handle);
    }

    // v15+ slots — wired at our bind version (23); overlay_key
    // forwards to QML, the rest are no-ops awaiting consumers.
    static void overlay_key(void *d, qdwin_shell_v1 *,
                            uint32_t role, uint32_t sym,
                            const char *utf8, uint32_t state) {
        auto *b = static_cast<QdwinBinding *>(d);
        b->overlayKeyCount_++;
        b->lastOverlayRole_ = role;
        b->lastOverlaySym_ = sym;
        b->lastOverlayUtf8_ = qstr(utf8);
        emit b->overlayKeyCountChanged();
        emit b->overlayKey(role, sym, b->lastOverlayUtf8_, state);
    }
    // spec/10 receive-time gate — forward to QML so ClipboardGate can
    // consult the broker (CheckClipboardReceive) and echo the verdict
    // back via sendDataOfferReceiveDecision. The compositor blocks the
    // receive() until we answer (or ~2s timeout → deny), so the QML
    // handler MUST answer exactly once on every path.
    static void data_offer_receive_pending(void *d, qdwin_shell_v1 *,
                                           uint32_t request_handle,
                                           const char *seat_name,
                                           uint32_t source_handle,
                                           uint32_t target_handle,
                                           const char *mime_type) {
        auto *b = static_cast<QdwinBinding *>(d);
        emit b->dataOfferReceivePending(request_handle, qstr(seat_name),
                                        source_handle, target_handle,
                                        qstr(mime_type));
    }
    // v19 hotkey — wired at v25. The shell registers WM-shortcut hotkeys
    // via registerHotkey() with shell-assigned ids and maps the id back to
    // a window-manager action in QML (WindowManagerService).
    static void hotkey_pressed(void *d, qdwin_shell_v1 *, uint32_t id) {
        emit static_cast<QdwinBinding *>(d)->hotkeyPressed(id);
    }
    // v29 appended a `serial` (last uint32_t) carrying the button's input
    // event serial, for a future show_popup() context-menu caller. Not yet
    // consumed here, but the dispatch signature must match the listener.
    static void chrome_button(void *, qdwin_shell_v1 *,
                              uint32_t, uint32_t, wl_fixed_t, wl_fixed_t,
                              uint32_t, uint32_t, uint32_t) {}
    static void popup_button(void *, qdwin_shell_v1 *,
                             uint32_t, wl_fixed_t, wl_fixed_t,
                             uint32_t, uint32_t, uint32_t) {}
    // v24 sidecar — which workspace a toplevel is on.
    static void toplevel_workspace(void *d, qdwin_shell_v1 *,
                                   uint32_t handle, uint32_t index) {
        emit static_cast<QdwinBinding *>(d)->toplevelWorkspace(handle, index);
    }
};

static const qdwin_shell_v1_listener kShellListener = {
    .hello                     = QdwinBindingDispatch::hello,
    .toplevel_added            = QdwinBindingDispatch::toplevel_added,
    .toplevel_geometry         = QdwinBindingDispatch::toplevel_geometry,
    .toplevel_state            = QdwinBindingDispatch::toplevel_state,
    .toplevel_title            = QdwinBindingDispatch::toplevel_title,
    .toplevel_removed          = QdwinBindingDispatch::toplevel_removed,
    .locked_changed            = QdwinBindingDispatch::locked_changed,
    .seat_created              = QdwinBindingDispatch::seat_created,
    .seat_removed              = QdwinBindingDispatch::seat_removed,
    .output_created            = QdwinBindingDispatch::output_created,
    .output_removed            = QdwinBindingDispatch::output_removed,
    .launcher_requested        = QdwinBindingDispatch::launcher_requested,
    .switcher_next             = QdwinBindingDispatch::switcher_next,
    .switcher_commit           = QdwinBindingDispatch::switcher_commit,
    .lock_requested            = QdwinBindingDispatch::lock_requested,
    .overlay_key               = QdwinBindingDispatch::overlay_key,
    .idle_lock_hint            = QdwinBindingDispatch::idle_lock_hint,
    .nested_proxy_pending      = QdwinBindingDispatch::nested_proxy_pending,
    .nested_proxy_pixel_source = QdwinBindingDispatch::nested_proxy_pixel_source,
    .selection_set             = QdwinBindingDispatch::selection_set,
    .selection_set_source_identity =
        QdwinBindingDispatch::selection_set_source_identity,
    .activation_pending        = QdwinBindingDispatch::activation_pending,
    .toplevel_security_context = QdwinBindingDispatch::toplevel_security_context,
    .toplevel_peer_identity    = QdwinBindingDispatch::toplevel_peer_identity,
    .seat_focus_changed        = QdwinBindingDispatch::seat_focus_changed,
    .data_offer_receive_pending = QdwinBindingDispatch::data_offer_receive_pending,
    .hotkey_pressed            = QdwinBindingDispatch::hotkey_pressed,
    .chrome_button             = QdwinBindingDispatch::chrome_button,
    .popup_button              = QdwinBindingDispatch::popup_button,
    .toplevel_workspace        = QdwinBindingDispatch::toplevel_workspace,
    .toplevel_app_id           = QdwinBindingDispatch::toplevel_app_id,
    .capture_served_stale      = QdwinBindingDispatch::capture_served_stale,
    .nested_proxy_remote_identity =
        QdwinBindingDispatch::nested_proxy_remote_identity,
    .remote_output_input_result =
        QdwinBindingDispatch::remote_output_input_result,
    .remote_output_drain_result =
        QdwinBindingDispatch::remote_output_drain_result,
};

// -------------------- ext-workspace-v1 client trampolines --------------------
//
// The standard workspace protocol. We bind the manager on the same
// wl_display as qdwin_shell_v1 (one notifier, one dispatch loop). The
// manager streams workspace_group + workspace handles and batches state
// with `done`; we collapse that into workspaceCount_ / activeWorkspace_
// on each done and emit workspacesChanged. Handle/group binding is routed
// through QdwinBinding members so the trampolines don't need to reference
// the listener globals defined below them.

struct QdwinWsDispatch {
    // ---- ext_workspace_handle_v1 ----
    static void h_id(void *, ext_workspace_handle_v1 *, const char *) {}
    static void h_name(void *, ext_workspace_handle_v1 *, const char *) {}
    static void h_coordinates(void *d, ext_workspace_handle_v1 *h,
                              wl_array *coords) {
        auto *b = static_cast<QdwinBinding *>(d);
        auto *e = b->wsEntryFor(h);
        if (e && coords && coords->size >= sizeof(uint32_t)) {
            e->coord = *static_cast<uint32_t *>(coords->data);
            e->haveCoord = true;
        }
    }
    static void h_state(void *d, ext_workspace_handle_v1 *h, uint32_t state) {
        auto *b = static_cast<QdwinBinding *>(d);
        auto *e = b->wsEntryFor(h);
        if (e) e->state = state;
    }
    static void h_capabilities(void *, ext_workspace_handle_v1 *, uint32_t) {}
    static void h_removed(void *d, ext_workspace_handle_v1 *h) {
        auto *b = static_cast<QdwinBinding *>(d);
        auto *e = b->wsEntryFor(h);
        if (e) e->removed = true;
    }
    // ---- ext_workspace_group_handle_v1 (single desktop-spanning group) ----
    static void g_capabilities(void *, ext_workspace_group_handle_v1 *, uint32_t) {}
    static void g_output_enter(void *, ext_workspace_group_handle_v1 *, wl_output *) {}
    static void g_output_leave(void *, ext_workspace_group_handle_v1 *, wl_output *) {}
    static void g_workspace_enter(void *, ext_workspace_group_handle_v1 *,
                                  ext_workspace_handle_v1 *) {}
    static void g_workspace_leave(void *, ext_workspace_group_handle_v1 *,
                                  ext_workspace_handle_v1 *) {}
    static void g_removed(void *, ext_workspace_group_handle_v1 *) {}
    // ---- ext_workspace_manager_v1 ----
    static void m_workspace_group(void *d, ext_workspace_manager_v1 *,
                                  ext_workspace_group_handle_v1 *grp) {
        static_cast<QdwinBinding *>(d)->wsBindGroup(grp);
    }
    static void m_workspace(void *d, ext_workspace_manager_v1 *,
                            ext_workspace_handle_v1 *ws) {
        static_cast<QdwinBinding *>(d)->wsBindHandle(ws);
    }
    static void m_done(void *d, ext_workspace_manager_v1 *) {
        static_cast<QdwinBinding *>(d)->wsRebuild();
    }
    static void m_finished(void *d, ext_workspace_manager_v1 *) {
        static_cast<QdwinBinding *>(d)->wsFinished();
    }
};

static const ext_workspace_handle_v1_listener kWsHandleListener = {
    .id           = QdwinWsDispatch::h_id,
    .name         = QdwinWsDispatch::h_name,
    .coordinates  = QdwinWsDispatch::h_coordinates,
    .state        = QdwinWsDispatch::h_state,
    .capabilities = QdwinWsDispatch::h_capabilities,
    .removed      = QdwinWsDispatch::h_removed,
};

static const ext_workspace_group_handle_v1_listener kWsGroupListener = {
    .capabilities    = QdwinWsDispatch::g_capabilities,
    .output_enter    = QdwinWsDispatch::g_output_enter,
    .output_leave    = QdwinWsDispatch::g_output_leave,
    .workspace_enter = QdwinWsDispatch::g_workspace_enter,
    .workspace_leave = QdwinWsDispatch::g_workspace_leave,
    .removed         = QdwinWsDispatch::g_removed,
};

static const ext_workspace_manager_v1_listener kWsManagerListener = {
    .workspace_group = QdwinWsDispatch::m_workspace_group,
    .workspace       = QdwinWsDispatch::m_workspace,
    .done            = QdwinWsDispatch::m_done,
    .finished        = QdwinWsDispatch::m_finished,
};

// ---- wlr-output-management-v1 client dispatch ----
// Mirror of QdwinWsDispatch: trampolines from the C listener structs into
// QdwinBinding member functions (QdwinOmDispatch is a friend).
struct QdwinOmDispatch {
    // ---- zwlr_output_mode_v1 ----
    static void md_size(void *d, zwlr_output_mode_v1 *m, int32_t w, int32_t h) {
        auto *b = static_cast<QdwinBinding *>(d);
        if (auto *e = b->omModeFor(m)) { e->width = w; e->height = h; }
    }
    static void md_refresh(void *d, zwlr_output_mode_v1 *m, int32_t r) {
        auto *b = static_cast<QdwinBinding *>(d);
        if (auto *e = b->omModeFor(m)) e->refresh = r;
    }
    static void md_preferred(void *d, zwlr_output_mode_v1 *m) {
        auto *b = static_cast<QdwinBinding *>(d);
        if (auto *e = b->omModeFor(m)) e->preferred = true;
    }
    static void md_finished(void *d, zwlr_output_mode_v1 *m) {
        auto *b = static_cast<QdwinBinding *>(d);
        if (auto *e = b->omModeFor(m)) {
            if (e->proxy) {
                zwlr_output_mode_v1_release(e->proxy);
                e->proxy = nullptr;
            }
        }
    }
    // ---- zwlr_output_head_v1 ----
    static void hd_name(void *d, zwlr_output_head_v1 *h, const char *n) {
        auto *b = static_cast<QdwinBinding *>(d);
        if (auto *e = b->omHeadFor(h)) e->name = qstr(n);
    }
    static void hd_description(void *d, zwlr_output_head_v1 *h, const char *s) {
        auto *b = static_cast<QdwinBinding *>(d);
        if (auto *e = b->omHeadFor(h)) e->description = qstr(s);
    }
    static void hd_physical_size(void *, zwlr_output_head_v1 *, int32_t, int32_t) {}
    static void hd_mode(void *d, zwlr_output_head_v1 *h, zwlr_output_mode_v1 *m) {
        auto *b = static_cast<QdwinBinding *>(d);
        if (auto *e = b->omHeadFor(h)) {
            QdwinBinding::OmModeInfo mi;
            mi.proxy = m;
            e->modes.push_back(mi);
            zwlr_output_mode_v1_add_listener(m, &kOmModeListener, b);
        }
    }
    static void hd_enabled(void *d, zwlr_output_head_v1 *h, int32_t en) {
        auto *b = static_cast<QdwinBinding *>(d);
        if (auto *e = b->omHeadFor(h)) e->enabled = (en != 0);
    }
    static void hd_current_mode(void *d, zwlr_output_head_v1 *h,
                                zwlr_output_mode_v1 *m) {
        auto *b = static_cast<QdwinBinding *>(d);
        if (auto *e = b->omHeadFor(h)) e->currentMode = m;
    }
    static void hd_position(void *d, zwlr_output_head_v1 *h, int32_t x, int32_t y) {
        auto *b = static_cast<QdwinBinding *>(d);
        if (auto *e = b->omHeadFor(h)) { e->x = x; e->y = y; }
    }
    static void hd_transform(void *d, zwlr_output_head_v1 *h, int32_t t) {
        auto *b = static_cast<QdwinBinding *>(d);
        if (auto *e = b->omHeadFor(h)) e->transform = t;
    }
    static void hd_scale(void *d, zwlr_output_head_v1 *h, wl_fixed_t s) {
        auto *b = static_cast<QdwinBinding *>(d);
        if (auto *e = b->omHeadFor(h)) {
            int sc = wl_fixed_to_int(s);
            e->scale = sc < 1 ? 1 : sc;
        }
    }
    static void hd_finished(void *d, zwlr_output_head_v1 *h) {
        // The head is now inert (the compositor destroyed it as part of a
        // resync or a hotplug-remove). Mark it so omRebuild() reaps it and
        // omSubmitLayout() never references the dead proxy. Per spec we send
        // a destroy request and release it.
        auto *b = static_cast<QdwinBinding *>(d);
        if (auto *e = b->omHeadFor(h)) {
            e->finished = true;
            if (e->proxy) {
                zwlr_output_head_v1_release(e->proxy);
                e->proxy = nullptr;
            }
        }
    }
    static void hd_make(void *d, zwlr_output_head_v1 *h, const char *s) {
        auto *b = static_cast<QdwinBinding *>(d);
        if (auto *e = b->omHeadFor(h)) e->make = qstr(s);
    }
    static void hd_model(void *d, zwlr_output_head_v1 *h, const char *s) {
        auto *b = static_cast<QdwinBinding *>(d);
        if (auto *e = b->omHeadFor(h)) e->model = qstr(s);
    }
    static void hd_serial(void *d, zwlr_output_head_v1 *h, const char *s) {
        auto *b = static_cast<QdwinBinding *>(d);
        if (auto *e = b->omHeadFor(h)) e->serial = qstr(s);
    }
    static void hd_adaptive_sync(void *, zwlr_output_head_v1 *, uint32_t) {}
    // ---- zwlr_output_manager_v1 ----
    static void mgr_head(void *d, zwlr_output_manager_v1 *,
                         zwlr_output_head_v1 *h) {
        static_cast<QdwinBinding *>(d)->omBindHead(h);
    }
    static void mgr_done(void *d, zwlr_output_manager_v1 *, uint32_t serial) {
        auto *b = static_cast<QdwinBinding *>(d);
        b->outputSerial_ = serial;
        b->omRebuild();
    }
    static void mgr_finished(void *d, zwlr_output_manager_v1 *) {
        static_cast<QdwinBinding *>(d)->omTeardownState();
    }
    // ---- zwlr_output_configuration_v1 ----
    static void cfg_succeeded(void *d, zwlr_output_configuration_v1 *c) {
        static_cast<QdwinBinding *>(d)->omConfigResult(c, true, false);
    }
    static void cfg_failed(void *d, zwlr_output_configuration_v1 *c) {
        static_cast<QdwinBinding *>(d)->omConfigResult(c, false, false);
    }
    static void cfg_cancelled(void *d, zwlr_output_configuration_v1 *c) {
        static_cast<QdwinBinding *>(d)->omConfigResult(c, false, true);
    }

    static const zwlr_output_mode_v1_listener kOmModeListener;
};

const zwlr_output_mode_v1_listener QdwinOmDispatch::kOmModeListener = {
    .size      = QdwinOmDispatch::md_size,
    .refresh   = QdwinOmDispatch::md_refresh,
    .preferred = QdwinOmDispatch::md_preferred,
    .finished  = QdwinOmDispatch::md_finished,
};

static const zwlr_output_head_v1_listener kOmHeadListener = {
    .name          = QdwinOmDispatch::hd_name,
    .description    = QdwinOmDispatch::hd_description,
    .physical_size  = QdwinOmDispatch::hd_physical_size,
    .mode           = QdwinOmDispatch::hd_mode,
    .enabled        = QdwinOmDispatch::hd_enabled,
    .current_mode   = QdwinOmDispatch::hd_current_mode,
    .position       = QdwinOmDispatch::hd_position,
    .transform      = QdwinOmDispatch::hd_transform,
    .scale          = QdwinOmDispatch::hd_scale,
    .finished       = QdwinOmDispatch::hd_finished,
    .make           = QdwinOmDispatch::hd_make,
    .model          = QdwinOmDispatch::hd_model,
    .serial_number  = QdwinOmDispatch::hd_serial,
    .adaptive_sync  = QdwinOmDispatch::hd_adaptive_sync,
};

static const zwlr_output_manager_v1_listener kOmManagerListener = {
    .head     = QdwinOmDispatch::mgr_head,
    .done     = QdwinOmDispatch::mgr_done,
    .finished = QdwinOmDispatch::mgr_finished,
};

static const zwlr_output_configuration_v1_listener kOmConfigListener = {
    .succeeded = QdwinOmDispatch::cfg_succeeded,
    .failed    = QdwinOmDispatch::cfg_failed,
    .cancelled = QdwinOmDispatch::cfg_cancelled,
};

// ---- ext-idle-notify-v1 (v26): idled / resumed -> idleStateChanged ----
// The listener user_data is the per-slot QdwinBinding::IdleSlot, which holds
// the binding back-pointer + the caller's slot index.
struct QdwinIdleDispatch {
    static void idled(void *d, ext_idle_notification_v1 *) {
        auto *s = static_cast<QdwinBinding::IdleSlot *>(d);
        if (s && s->self) emit s->self->idleStateChanged(s->slot, true);
    }
    static void resumed(void *d, ext_idle_notification_v1 *) {
        auto *s = static_cast<QdwinBinding::IdleSlot *>(d);
        if (s && s->self) emit s->self->idleStateChanged(s->slot, false);
    }
};

static const ext_idle_notification_v1_listener kIdleListener = {
    QdwinIdleDispatch::idled,
    QdwinIdleDispatch::resumed,
};

// ---- shell-authorized output capture discovery (v32) -------------------

struct QdwinBinding::CaptureOutput {
    QdwinBinding *binding = nullptr;
    uint32_t globalName = 0;
    wl_output *proxy = nullptr;
    QString name;
};

struct QdwinCaptureOutputDispatch {
    static void geometry(void *, wl_output *, int32_t, int32_t, int32_t,
                         int32_t, int32_t, const char *, const char *,
                         int32_t) {}
    static void mode(void *, wl_output *, uint32_t, int32_t, int32_t,
                     int32_t) {}
    static void done(void *, wl_output *) {}
    static void scale(void *, wl_output *, int32_t) {}
    static void name(void *data, wl_output *, const char *name) {
        auto *output = static_cast<QdwinBinding::CaptureOutput *>(data);
        output->name = qstr(name);
    }
    static void description(void *, wl_output *, const char *) {}
};

static const wl_output_listener kCaptureOutputListener = {
    QdwinCaptureOutputDispatch::geometry,
    QdwinCaptureOutputDispatch::mode,
    QdwinCaptureOutputDispatch::done,
    QdwinCaptureOutputDispatch::scale,
    QdwinCaptureOutputDispatch::name,
    QdwinCaptureOutputDispatch::description,
};

struct QdwinCaptureRegistry {
    static void global(void *data, wl_registry *registry, uint32_t name,
                       const char *interface, uint32_t version) {
        auto *binding = static_cast<QdwinBinding *>(data);
        if (std::strcmp(interface, weston_capture_v1_interface.name) == 0) {
            if (!binding->capture_ && version >= 2) {
                binding->capture_ = static_cast<weston_capture_v1 *>(
                    wl_registry_bind(registry, name,
                                     &weston_capture_v1_interface, 2));
                binding->captureGlobalName_ = name;
            }
            return;
        }
        if (std::strcmp(interface, wl_shm_interface.name) == 0) {
            if (!binding->captureShm_) {
                binding->captureShm_ = static_cast<wl_shm *>(
                    wl_registry_bind(registry, name, &wl_shm_interface, 1));
                binding->captureShmGlobalName_ = name;
            }
            return;
        }
        if (std::strcmp(interface, wl_output_interface.name) == 0) {
            // wl_output.name is a v4 event; unnamed older outputs cannot be
            // safely selected and are intentionally not bound for capture.
            if (version < 4)
                return;
            auto output = std::make_unique<QdwinBinding::CaptureOutput>();
            output->binding = binding;
            output->globalName = name;
            output->proxy = static_cast<wl_output *>(
                wl_registry_bind(registry, name, &wl_output_interface, 4));
            wl_output_add_listener(output->proxy, &kCaptureOutputListener,
                                   output.get());
            binding->captureOutputs_.push_back(std::move(output));
        }
    }

    static void global_remove(void *data, wl_registry *, uint32_t name) {
        auto *binding = static_cast<QdwinBinding *>(data);
        if (name == binding->captureGlobalName_) {
            if (binding->capture_)
                weston_capture_v1_destroy(binding->capture_);
            binding->capture_ = nullptr;
            binding->captureGlobalName_ = 0;
        }
        if (name == binding->captureShmGlobalName_) {
            if (binding->captureShm_)
                // Proxy-only destroy: wl_shm is bound at v1 and the
                // release request only exists since v2.
                wl_shm_destroy(binding->captureShm_);
            binding->captureShm_ = nullptr;
            binding->captureShmGlobalName_ = 0;
        }
        auto it = std::remove_if(
            binding->captureOutputs_.begin(), binding->captureOutputs_.end(),
            [name](const std::unique_ptr<QdwinBinding::CaptureOutput> &output) {
                if (output->globalName != name)
                    return false;
                if (output->proxy)
                    wl_output_release(output->proxy);
                return true;
            });
        binding->captureOutputs_.erase(it, binding->captureOutputs_.end());
    }
};

static const wl_registry_listener kCaptureRegistryListener = {
    QdwinCaptureRegistry::global,
    QdwinCaptureRegistry::global_remove,
};

struct CaptureJob {
    std::vector<uint32_t> formats;
    int width = 0;
    int height = 0;
    bool formatsDone = false;
    bool complete = false;
    bool retry = false;
    bool failed = false;
    QString error;
};

struct CaptureJobDispatch {
    static void format(void *data, weston_capture_source_v1 *,
                       uint32_t drmFormat) {
        auto *job = static_cast<CaptureJob *>(data);
        // A new format batch after formats_done supersedes the old one.
        if (job->formatsDone) {
            job->formats.clear();
            job->formatsDone = false;
        }
        if (std::find(job->formats.begin(), job->formats.end(), drmFormat) ==
            job->formats.end())
            job->formats.push_back(drmFormat);
    }
    static void size(void *data, weston_capture_source_v1 *, int32_t width,
                     int32_t height) {
        auto *job = static_cast<CaptureJob *>(data);
        job->width = width;
        job->height = height;
    }
    static void complete(void *data, weston_capture_source_v1 *) {
        static_cast<CaptureJob *>(data)->complete = true;
    }
    static void retry(void *data, weston_capture_source_v1 *) {
        static_cast<CaptureJob *>(data)->retry = true;
    }
    static void failed(void *data, weston_capture_source_v1 *,
                       const char *message) {
        auto *job = static_cast<CaptureJob *>(data);
        job->failed = true;
        job->error = message ? qstr(message)
                             : QStringLiteral("capture failed without reason");
    }
    static void formats_done(void *data, weston_capture_source_v1 *) {
        static_cast<CaptureJob *>(data)->formatsDone = true;
    }
};

static const weston_capture_source_v1_listener kCaptureSourceListener = {
    CaptureJobDispatch::format,
    CaptureJobDispatch::size,
    CaptureJobDispatch::complete,
    CaptureJobDispatch::retry,
    CaptureJobDispatch::failed,
    CaptureJobDispatch::formats_done,
};

// wl_registry global handler — looks for qdwin_shell_v1 specifically.
// QdwinBindingDispatch is already a friend of QdwinBinding so it can
// write shell_ / shellVersion_ directly. We piggyback the registry
// callbacks on the same struct rather than introducing a second friend.
struct QdwinRegistry {
    static void global(void *data, wl_registry *reg, uint32_t name,
                       const char *interface, uint32_t version) {
        auto *b = static_cast<QdwinBinding *>(data);
        if (std::strcmp(interface, qdwin_shell_v1_interface.name) == 0) {
            uint32_t v = version < kBindVersion ? version : kBindVersion;
            auto *proxy = static_cast<qdwin_shell_v1 *>(
                wl_registry_bind(reg, name, &qdwin_shell_v1_interface, v));
            b->shell_ = proxy;
            b->shellVersion_ = v;
            return;
        }
        // v24: standard workspace protocol (advertised to all clients).
        if (std::strcmp(interface, ext_workspace_manager_v1_interface.name) == 0) {
            auto *mgr = static_cast<ext_workspace_manager_v1 *>(
                wl_registry_bind(reg, name,
                                 &ext_workspace_manager_v1_interface, 1));
            b->wsManager_ = mgr;
            ext_workspace_manager_v1_add_listener(mgr, &kWsManagerListener, b);
            return;
        }
        // Output (display) management (advertised to all clients).
        if (std::strcmp(interface, zwlr_output_manager_v1_interface.name) == 0) {
            uint32_t v = version < 4 ? version : 4;
            auto *mgr = static_cast<zwlr_output_manager_v1 *>(
                wl_registry_bind(reg, name,
                                 &zwlr_output_manager_v1_interface, v));
            b->omBindManager(mgr);
            return;
        }
        // v26 idle/DPMS: a wl_seat (for get_idle_notification) +
        // ext_idle_notifier_v1. Both are needed before PowerService can arm
        // idle notifications, so flag availability once both are present.
        if (std::strcmp(interface, wl_seat_interface.name) == 0) {
            if (!b->seat_) {
                uint32_t v = version < 5 ? version : 5;
                b->seat_ = static_cast<wl_seat *>(
                    wl_registry_bind(reg, name, &wl_seat_interface, v));
                b->seatName_ = name;
                emit b->idleNotifierAvailableChanged();
            }
            return;
        }
        if (std::strcmp(interface, ext_idle_notifier_v1_interface.name) == 0) {
            if (!b->idleNotifier_) {
                b->idleNotifier_ = static_cast<ext_idle_notifier_v1 *>(
                    wl_registry_bind(reg, name,
                                     &ext_idle_notifier_v1_interface, 1));
                b->idleNotifierName_ = name;
                emit b->idleNotifierAvailableChanged();
            }
            return;
        }
    }
    static void global_remove(void *data, wl_registry *, uint32_t name) {
        static_cast<QdwinBinding *>(data)->idleGlobalRemoved(name);
    }
};

static const wl_registry_listener kRegistryListener = {
    QdwinRegistry::global,
    QdwinRegistry::global_remove,
};

// -------------------- QdwinBinding --------------------

QdwinBinding::QdwinBinding(QObject *parent) : QObject(parent) {
    reconnectTimer_.setSingleShot(true);
    connect(&reconnectTimer_, &QTimer::timeout, this, [this]() {
        if (destroying_) return;
        qWarning().noquote() << "qdwin-binding: reconnect attempt"
                             << reconnectAttempts_;
        connectAndBind();
    });
    // Stability gate: a (re)bind only resets the reconnect backoff once it has
    // survived the grace period (see stableTimer_ in the header + setBound).
    stableTimer_.setSingleShot(true);
    connect(&stableTimer_, &QTimer::timeout, this, [this]() {
        if (bound_) reconnectAttempts_ = 0;
    });
    // Defer the initial connect to the live event loop. connectAndBind()
    // now performs synchronous roundtrips that dispatch hello and qdwin's
    // bind replay; QML attaches onBoundChanged / protocol handlers only
    // after this constructor returns, so a synchronous connect here would
    // emit those signals into the void (ClipboardGate, WM policy, replay
    // state would never initialize). Queued invocation runs once the QML
    // object is complete — the same ordering the pre-capture async-hello
    // implementation guaranteed.
    QMetaObject::invokeMethod(this, [this]() {
        if (!destroying_) connectAndBind();
    }, Qt::QueuedConnection);

    ctrlServer_ = new CtrlServer(*this, this);
}

QdwinBinding::~QdwinBinding() {
    destroying_ = true;
    reconnectTimer_.stop();
    stableTimer_.stop();
    teardown(QStringLiteral("binding destroyed"));
}

void QdwinBinding::connectAndBind() {
    display_ = wl_display_connect(nullptr);
    if (!display_) {
        setLastError(QStringLiteral(
            "wl_display_connect failed (WAYLAND_DISPLAY=%1, errno=%2: %3)")
            .arg(qstr(std::getenv("WAYLAND_DISPLAY")))
            .arg(errno).arg(qstr(std::strerror(errno))));
        emit disconnected();
        // Re-arm the (one-shot) reconnect timer ourselves. teardown() is the
        // usual path that schedules a reconnect, but there are no display
        // resources to tear down here, so it isn't called — and without this a
        // scheduled reconnect that races the compositor still being unavailable
        // (socket not yet up after a restart) would consume the one-shot timer
        // and leave the binding stuck unbound forever. Backoff still applies.
        if (!destroying_) scheduleReconnect();
        return;
    }

    registry_ = wl_display_get_registry(display_);
    wl_registry_add_listener(registry_, &kRegistryListener, this);
    wl_display_roundtrip(display_);

    if (!shell_) {
        setLastError(QStringLiteral(
            "qdwin_shell_v1 global not advertised on this display"));
        teardown(lastError_);
        return;
    }

    qdwin_shell_v1_add_listener(shell_, &kShellListener, this);
    qdwin_shell_v1_bind_as_shell(shell_);

    // Complete bind_as_shell first. The synchronous roundtrip dispatches
    // hello and therefore changes this exact wl_client's qdwin credential to
    // SHELL before we ask for a fresh registry enumeration.
    if (wl_display_roundtrip(display_) == -1 || !bound_) {
        setLastError(QStringLiteral(
            "roundtrip after bind_as_shell failed or hello was not received"));
        teardown(lastError_);
        return;
    }

    // A second registry is mandatory: the first enumeration happened while
    // this connection was still ordinary, so qdwin's global filter omitted
    // weston_capture_v1. Credential changes do not replay old globals. The
    // first roundtrip below records/binds all globals; the second receives
    // wl_output.name and other events caused by those binds, ensuring output
    // selection is complete before any capture source can be created.
    captureRegistry_ = wl_display_get_registry(display_);
    wl_registry_add_listener(captureRegistry_, &kCaptureRegistryListener, this);
    if (wl_display_roundtrip(display_) == -1 ||
        wl_display_roundtrip(display_) == -1) {
        setLastError(QStringLiteral(
            "shell capture registry enumeration failed"));
        teardown(lastError_);
        return;
    }
    if (!capture_ || !captureShm_) {
        qWarning().noquote()
            << "qdwin-binding: shell capture unavailable:"
            << (!capture_ ? "weston_capture_v1 missing or older than v2" : "")
            << (!captureShm_ ? "wl_shm missing" : "");
    }

    readNotifier_ = new QSocketNotifier(wl_display_get_fd(display_),
                                        QSocketNotifier::Read, this);
    connect(readNotifier_, &QSocketNotifier::activated,
            this, &QdwinBinding::onWaylandReadable);
}

void QdwinBinding::onWaylandReadable() {
    if (!display_) return;

    // wl_display_dispatch reads + dispatches; non-blocking when the fd
    // is readable, which QSocketNotifier guarantees here.
    int n = wl_display_dispatch(display_);
    if (n == -1) {
        setLastError(QStringLiteral("wl_display_dispatch failed: errno=%1: %2")
                     .arg(errno).arg(qstr(std::strerror(errno))));
        teardown(lastError_);
        return;
    }
    // Flush so any outgoing requests written from QML during dispatch
    // (e.g. focusWindow called from a signal handler) reach the socket.
    if (wl_display_flush(display_) == -1 && errno != EAGAIN) {
        setLastError(QStringLiteral("wl_display_flush failed: errno=%1: %2")
                     .arg(errno).arg(qstr(std::strerror(errno))));
        teardown(lastError_);
        return;
    }
}

void QdwinBinding::teardown(const QString &reason) {
    if (readNotifier_) {
        readNotifier_->setEnabled(false);
        readNotifier_->deleteLater();
        readNotifier_ = nullptr;
    }
    if (display_) {
        wl_display_disconnect(display_);
        display_ = nullptr;
    }
    registry_ = nullptr;
    shell_ = nullptr;
    captureTeardownState();
    wsTeardownState();
    omTeardownState();
    idleTeardownState();
    if (bound_) setBound(false);
    if (!reason.isEmpty() && lastError_.isEmpty())
        setLastError(reason);
    emit disconnected();
    // Schedule a reconnect on any non-destructor teardown — covers
    // qdwin/weston restarts, transient broken-pipe on the wayland
    // socket, and bind-time failures (display not yet up). The
    // destructor sets destroying_ so we don't fire after delete.
    if (!destroying_) scheduleReconnect();
}

void QdwinBinding::captureTeardownState() {
    // Called only after wl_display_disconnect(), so the connection has
    // already destroyed every proxy. Drop raw pointers without marshaling
    // destructor requests onto the dead display.
    captureRegistry_ = nullptr;
    capture_ = nullptr;
    captureShm_ = nullptr;
    captureGlobalName_ = 0;
    captureShmGlobalName_ = 0;
    captureOutputs_.clear();
    captureBusy_ = false;
}

void QdwinBinding::scheduleReconnect() {
    if (destroying_) return;
    // Exponential backoff with a cap so we don't busy-loop if the
    // compositor never comes back. 200 ms → 400 ms → 800 ms → … →
    // 5000 ms ceiling. Reset on a successful hello (setBound(true)).
    int ms = 200;
    for (int i = 0; i < reconnectAttempts_ && ms < 5000; ++i) ms *= 2;
    if (ms > 5000) ms = 5000;
    reconnectAttempts_++;
    reconnectTimer_.start(ms);
}

void QdwinBinding::setLastError(const QString &s) {
    if (lastError_ == s) return;
    lastError_ = s;
    qWarning().noquote() << "qdwin-binding: error:" << s;
    emit lastErrorChanged();
}

// Grace period a (re)connection must stay bound before its backoff is reset.
// Matches the scheduleReconnect() ceiling so a genuinely stable connection
// resets promptly while a sub-second flap never does.
static constexpr int kReconnectStableMs = 5000;

void QdwinBinding::setBound(bool b) {
    if (bound_ == b) return;
    bound_ = b;
    if (b) {
        // Do NOT reset the reconnect backoff here. A hello only means the
        // connection bound this instant; under a teardown/reconnect flap the
        // fresh connection routinely dies again within milliseconds (the
        // onBound capability re-assert burst re-errors it after a fatal
        // protocol error like ERROR_LOCKED). Resetting reconnectAttempts_ on
        // every brief bind pins the backoff at 200 ms and turns one fatal
        // error into a perpetual reconnect storm. Instead arm the stability
        // timer; only a connection that survives kReconnectStableMs is treated
        // as stable and resets the backoff (see stableTimer_).
        stableTimer_.start(kReconnectStableMs);
        lastError_.clear();
    } else {
        // Unbound again before proving stable — keep the elevated backoff so
        // scheduleReconnect() spaces the next attempt out (storm self-limits).
        stableTimer_.stop();
    }
    emit boundChanged();
}

void QdwinBinding::setFocused(const QString &seat, quint32 handle) {
    if (focusedSeat_ == seat && focusedHandle_ == handle) return;
    focusedSeat_ = seat;
    focusedHandle_ = handle;
    emit focusedHandleChanged();
}

// -------- imperative requests ----------

// Flush after an imperative request and HONOUR the result. Every imperative
// qdwin_shell_v1 request below is fire-and-forget from QML, then flushed so the
// write actually hits the socket. A flush that fails with EAGAIN is benign —
// the kernel socket buffer is momentarily full, libwayland keeps the request
// queued and a later flush (or the read-path flush in onWaylandReadable)
// drains it. Any OTHER error (EPIPE/ECONNRESET — the deny-storm overran
// libwayland's 4 KB buffer and the connection is fatally errored) means the
// request did NOT and will NOT reach the compositor: we must tear the binding
// down and reconnect rather than silently pretend it was sent (which is how a
// load-bearing set_keyboard_focus/clear_selection got dropped under load). See
// clipboard.md §"deny-storm robustness". `requestName` (pass __func__) names
// the request in the error for diagnostics. Returns true if the write is on its
// way (sent or queued), false if the binding was torn down.
bool QdwinBinding::flushAfterRequest(const char *requestName) {
    if (!display_)
        return false;
    if (wl_display_flush(display_) != -1)
        return true;
    if (errno == EAGAIN)
        return true;
    setLastError(QStringLiteral("%1: wl_display_flush failed: errno=%2: %3")
                     .arg(qstr(requestName))
                     .arg(errno)
                     .arg(qstr(std::strerror(errno))));
    teardown(lastError_);
    return false;
}

QVariantMap QdwinBinding::captureOutput(const QString &outputName,
                                        const QString &destPath,
                                        int timeoutMs) {
    const int captureTimeoutMs =
        (timeoutMs > 0) ? timeoutMs : kCaptureTimeoutMs;
    auto failure = [](const QString &error) {
        qWarning().noquote() << "qdwin-binding: capture failed:" << error;
        return QVariantMap{
            {QStringLiteral("ok"), false},
            {QStringLiteral("error"), error},
        };
    };

    if (captureBusy_)
        return failure(QStringLiteral("capture already in progress"));
    captureBusy_ = true;
    captureStaleServed_ = false;
    captureStaleAgeMs_ = 0;
    captureStaleMsc_ = 0;
    struct BusyReset {
        bool &busy;
        ~BusyReset() { busy = false; }
    } busyReset{captureBusy_};

    if (!display_ || !bound_ || !shell_)
        return failure(QStringLiteral("qdshell is not bound to qdwin"));
    if (shellVersion_ < 32)
        return failure(QStringLiteral(
            "qdwin_shell_v1 v32 prepare_output_capture is unavailable"));
    if (!capture_ || !captureShm_)
        return failure(QStringLiteral(
            "weston_capture_v1 v2 or wl_shm is unavailable"));
    if (outputName.isEmpty())
        return failure(QStringLiteral("output name is empty"));
    // Mirror the compositor authority's exact-output pin (Virtual-1, the
    // main head of the headless test VMs). Not a security boundary here —
    // qdwin enforces it independently — but it keeps refusal local and the
    // contract visible: this facility captures one designated output only.
    if (outputName != QStringLiteral("Virtual-1"))
        return failure(QStringLiteral(
            "refusing capture of non-designated output %1 (only Virtual-1)")
                           .arg(outputName));
    if (!QDir::isAbsolutePath(destPath))
        return failure(QStringLiteral("destination path must be absolute"));

    QByteArray destBytes = QFile::encodeName(destPath);
    struct stat destStat;
    if (::lstat(destBytes.constData(), &destStat) == 0)
        return failure(QStringLiteral(
            "destination already exists (refusing stale capture): %1")
                           .arg(destPath));
    if (errno != ENOENT)
        return failure(QStringLiteral("cannot inspect destination %1: %2")
                           .arg(destPath, qstr(std::strerror(errno))));

    QString runtimeDir = qstr(std::getenv("XDG_RUNTIME_DIR"));
    if (runtimeDir.isEmpty() || !QDir::isAbsolutePath(runtimeDir))
        return failure(QStringLiteral("XDG_RUNTIME_DIR is missing or invalid"));
    QFileInfo parentInfo(destPath);
    QString destDir = parentInfo.absoluteDir().absolutePath();
    struct stat runtimeStat;
    struct stat destDirStat;
    QByteArray runtimeBytes = QFile::encodeName(runtimeDir);
    QByteArray destDirBytes = QFile::encodeName(destDir);
    if (::stat(runtimeBytes.constData(), &runtimeStat) != 0 ||
        ::stat(destDirBytes.constData(), &destDirStat) != 0)
        return failure(QStringLiteral(
            "capture runtime or destination directory is unavailable"));
    if (runtimeStat.st_dev != destDirStat.st_dev)
        return failure(QStringLiteral(
            "destination must share a filesystem with XDG_RUNTIME_DIR for atomic rename"));

    wl_output *targetOutput = nullptr;
    int matches = 0;
    for (const auto &output : captureOutputs_) {
        if (output->name == outputName) {
            targetOutput = output->proxy;
            ++matches;
        }
    }
    if (matches == 0)
        return failure(QStringLiteral("output not found: %1").arg(outputName));
    if (matches != 1)
        return failure(QStringLiteral("output name is ambiguous: %1")
                           .arg(outputName));

    QByteArray outputUtf8 = outputName.toUtf8();
    qInfo().noquote() << "qdwin-binding: capture starting output="
                      << outputName << "path=" << destPath;
    qdwin_shell_v1_prepare_output_capture(shell_, outputUtf8.constData());
    if (!flushAfterRequest(__func__))
        return failure(QStringLiteral("failed to queue output damage"));

    CaptureJob job;
    weston_capture_source_v1 *source = weston_capture_v1_create(
        capture_, targetOutput, WESTON_CAPTURE_V1_SOURCE_FRAMEBUFFER);
    if (!source)
        return failure(QStringLiteral("failed to create capture source"));
    weston_capture_source_v1_add_listener(source, &kCaptureSourceListener,
                                          &job);

    // On an idle DRM output libweston may not have populated framebuffer
    // source requirements yet. The first preparation request is deliberately
    // ordered before source creation; damage once more now that the source is
    // on libweston's capture_source_list so the repaint publishes its
    // format/size events. This remains before the actual capture(buffer)
    // request and is harmless when source info was already available.
    qdwin_shell_v1_prepare_output_capture(shell_, outputUtf8.constData());
    if (!flushAfterRequest(__func__)) {
        if (display_ && wl_display_get_error(display_) == 0)
            weston_capture_source_v1_destroy(source);
        return failure(QStringLiteral(
            "failed to queue source-discovery output damage"));
    }

    struct CaptureBuffer {
        std::unique_ptr<QTemporaryFile> backing;
        wl_buffer *proxy = nullptr;
        void *pixels = MAP_FAILED;
        size_t size = 0;
        int stride = 0;
        uint32_t drmFormat = 0;
        QImage::Format imageFormat = QImage::Format_Invalid;
    } buffer;

    bool displayFailed = false;
    auto destroyBuffer = [&]() {
        if (buffer.proxy && display_ && wl_display_get_error(display_) == 0)
            wl_buffer_destroy(buffer.proxy);
        buffer.proxy = nullptr;
        if (buffer.pixels != MAP_FAILED)
            ::munmap(buffer.pixels, buffer.size);
        buffer.pixels = MAP_FAILED;
        buffer.size = 0;
        buffer.backing.reset();
    };
    auto destroySource = [&]() {
        destroyBuffer();
        if (source && display_ && wl_display_get_error(display_) == 0) {
            weston_capture_source_v1_destroy(source);
            wl_display_flush(display_);
        }
        source = nullptr;
    };

    QElapsedTimer deadline;
    deadline.start();
    QString pumpError;
    auto pumpUntil = [&](auto done) {
        while (!done()) {
            if (wl_display_dispatch_pending(display_) == -1) {
                pumpError = QStringLiteral(
                    "Wayland dispatch failed while capturing: %1")
                                .arg(qstr(std::strerror(errno)));
                displayFailed = true;
                return false;
            }
            if (done())
                return true;

            short events = POLLIN;
            if (wl_display_flush(display_) == -1) {
                if (errno != EAGAIN) {
                    pumpError = QStringLiteral(
                        "Wayland flush failed while capturing: %1")
                                    .arg(qstr(std::strerror(errno)));
                    displayFailed = true;
                    return false;
                }
                events |= POLLOUT;
            }

            qint64 remaining = captureTimeoutMs - deadline.elapsed();
            if (remaining <= 0) {
                pumpError = QStringLiteral("capture timed out after %1 ms")
                                .arg(captureTimeoutMs);
                return false;
            }
            pollfd pfd{wl_display_get_fd(display_), events, 0};
            int rc;
            do {
                rc = ::poll(&pfd, 1, static_cast<int>(remaining));
            } while (rc < 0 && errno == EINTR);
            if (rc == 0) {
                pumpError = QStringLiteral("capture timed out after %1 ms")
                                .arg(captureTimeoutMs);
                return false;
            }
            if (rc < 0) {
                pumpError = QStringLiteral("poll failed while capturing: %1")
                                .arg(qstr(std::strerror(errno)));
                return false;
            }
            if (pfd.revents & (POLLERR | POLLHUP | POLLNVAL)) {
                pumpError = QStringLiteral(
                    "Wayland connection closed while capturing");
                displayFailed = true;
                return false;
            }
            if ((pfd.revents & POLLIN) && wl_display_dispatch(display_) == -1) {
                pumpError = QStringLiteral(
                    "Wayland dispatch failed while capturing: %1")
                                .arg(qstr(std::strerror(errno)));
                displayFailed = true;
                return false;
            }
        }
        return true;
    };

    if (!pumpUntil([&]() {
            return job.failed ||
                   (job.formatsDone && job.width > 0 && job.height > 0);
        })) {
        destroySource();
        if (displayFailed)
            teardown(pumpError);
        return failure(pumpError);
    }
    if (job.failed) {
        QString error = QStringLiteral("capture source failed: %1")
                            .arg(job.error);
        destroySource();
        return failure(error);
    }

    auto allocateBuffer = [&]() {
        uint32_t wlFormat = 0;
        buffer.imageFormat = QImage::Format_Invalid;
        // Prefer opaque XRGB. Both ARGB/XRGB are mandatory wl_shm formats;
        // their little-endian byte layout maps directly to Qt's native
        // RGB32/ARGB32 representations.
        if (std::find(job.formats.begin(), job.formats.end(),
                      kDrmXrgb8888) != job.formats.end()) {
            buffer.drmFormat = kDrmXrgb8888;
            wlFormat = WL_SHM_FORMAT_XRGB8888;
            buffer.imageFormat = QImage::Format_RGB32;
        } else if (std::find(job.formats.begin(), job.formats.end(),
                             kDrmArgb8888) != job.formats.end()) {
            buffer.drmFormat = kDrmArgb8888;
            wlFormat = WL_SHM_FORMAT_ARGB8888;
            buffer.imageFormat = QImage::Format_ARGB32;
        } else {
            pumpError = QStringLiteral(
                "capture source offers no supported XRGB8888/ARGB8888 format");
            return false;
        }
        if (job.width <= 0 || job.height <= 0 ||
            job.width > std::numeric_limits<int>::max() / 4) {
            pumpError = QStringLiteral("invalid capture dimensions %1x%2")
                            .arg(job.width).arg(job.height);
            return false;
        }
        buffer.stride = job.width * 4;
        if (static_cast<quint64>(buffer.stride) *
                static_cast<quint64>(job.height) >
            static_cast<quint64>(std::numeric_limits<int>::max())) {
            pumpError = QStringLiteral("capture buffer is too large");
            return false;
        }
        buffer.size = static_cast<size_t>(buffer.stride) * job.height;
        buffer.backing = std::make_unique<QTemporaryFile>(
            runtimeDir + QStringLiteral("/.qdshell-capture-shm-XXXXXX"));
        if (!buffer.backing->open() ||
            !buffer.backing->setPermissions(QFileDevice::ReadOwner |
                                            QFileDevice::WriteOwner) ||
            !buffer.backing->resize(static_cast<qint64>(buffer.size))) {
            pumpError = QStringLiteral("failed to allocate capture shm file: %1")
                            .arg(buffer.backing->errorString());
            return false;
        }
        buffer.pixels = ::mmap(nullptr, buffer.size, PROT_READ | PROT_WRITE,
                               MAP_SHARED, buffer.backing->handle(), 0);
        if (buffer.pixels == MAP_FAILED) {
            pumpError = QStringLiteral("mmap capture buffer failed: %1")
                            .arg(qstr(std::strerror(errno)));
            return false;
        }
        wl_shm_pool *pool = wl_shm_create_pool(
            captureShm_, buffer.backing->handle(),
            static_cast<int32_t>(buffer.size));
        if (!pool) {
            pumpError = QStringLiteral("wl_shm_create_pool failed");
            return false;
        }
        buffer.proxy = wl_shm_pool_create_buffer(
            pool, 0, job.width, job.height, buffer.stride, wlFormat);
        wl_shm_pool_destroy(pool);
        if (!buffer.proxy) {
            pumpError = QStringLiteral("wl_shm_pool_create_buffer failed");
            return false;
        }
        return true;
    };

    int retries = 0;
    for (;;) {
        if (!allocateBuffer()) {
            destroySource();
            return failure(pumpError);
        }
        job.complete = false;
        job.retry = false;
        job.failed = false;
        job.error.clear();
        weston_capture_source_v1_capture(source, buffer.proxy);
        // The capture request itself only schedule_repaint()s. On this idle
        // DRM VM the earlier full damage can already have been consumed while
        // we waited for source format/size. Queue another v32 preparation
        // immediately AFTER capture(buffer) on the same connection: server
        // request order guarantees the capture task exists before full damage
        // starts the servicing repaint.
        qdwin_shell_v1_prepare_output_capture(shell_, outputUtf8.constData());
        if (wl_display_flush(display_) == -1 && errno != EAGAIN) {
            pumpError = QStringLiteral("failed to flush capture request: %1")
                            .arg(qstr(std::strerror(errno)));
            displayFailed = true;
            destroySource();
            teardown(pumpError);
            return failure(pumpError);
        }
        if (!pumpUntil([&]() {
                return job.complete || job.retry || job.failed;
            })) {
            destroySource();
            if (displayFailed)
                teardown(pumpError);
            return failure(pumpError);
        }
        if (job.failed) {
            QString error = QStringLiteral("capture failed: %1").arg(job.error);
            destroySource();
            return failure(error);
        }
        if (!job.retry)
            break;
        if (retries++ >= 1) {
            destroySource();
            return failure(QStringLiteral(
                "capture requirements changed more than once"));
        }
        destroyBuffer();
        qdwin_shell_v1_prepare_output_capture(shell_, outputUtf8.constData());
        if (!flushAfterRequest(__func__)) {
            destroySource();
            return failure(QStringLiteral(
                "failed to queue output damage for capture retry"));
        }
    }

    QImage image(static_cast<uchar *>(buffer.pixels), job.width, job.height,
                 buffer.stride, buffer.imageFormat);
    if (image.isNull()) {
        destroySource();
        return failure(QStringLiteral("QImage rejected captured pixels"));
    }

    QTemporaryFile pngFile(
        runtimeDir + QStringLiteral("/.qdshell-capture-XXXXXX.png"));
    pngFile.setAutoRemove(true);
    if (!pngFile.open() ||
        !pngFile.setPermissions(QFileDevice::ReadOwner |
                                QFileDevice::WriteOwner) ||
        !image.save(&pngFile, "PNG") || !pngFile.flush() ||
        ::fsync(pngFile.handle()) != 0) {
        QString error = QStringLiteral("failed to encode capture PNG: %1")
                            .arg(pngFile.errorString());
        destroySource();
        return failure(error);
    }
    const QString tmpPath = pngFile.fileName();
    pngFile.setAutoRemove(false);
    pngFile.close();

    QByteArray tmpBytes = QFile::encodeName(tmpPath);
    // RENAME_NOREPLACE makes the earlier lstat(dest)==ENOENT check exact:
    // a file created at dest between the check and the publish fails the
    // capture instead of being silently replaced.
    if (::renameat2(AT_FDCWD, tmpBytes.constData(),
                    AT_FDCWD, destBytes.constData(),
                    RENAME_NOREPLACE) != 0) {
        QString error = QStringLiteral("atomic no-replace rename to %1 failed: %2")
                            .arg(destPath, qstr(std::strerror(errno)));
        QFile::remove(tmpPath);
        destroySource();
        return failure(error);
    }
    int dirFd = ::open(destDirBytes.constData(), O_RDONLY | O_DIRECTORY);
    if (dirFd >= 0) {
        ::fsync(dirFd);
        ::close(dirFd);
    }

    destroySource();
    qInfo().noquote() << "qdwin-binding: capture complete output="
                      << outputName << "size=" << job.width << "x"
                      << job.height << "path=" << destPath
                      << (captureStaleServed_ ? "STALE" : "live");
    return {
        {QStringLiteral("ok"), true},
        {QStringLiteral("width"), job.width},
        {QStringLiteral("height"), job.height},
        {QStringLiteral("output"), outputName},
        {QStringLiteral("path"), destPath},
        {QStringLiteral("live"), !captureStaleServed_},
        {QStringLiteral("staleAgeMs"),
         static_cast<qulonglong>(captureStaleAgeMs_)},
        {QStringLiteral("staleMsc"),
         static_cast<qulonglong>(captureStaleMsc_)},
    };
}

void QdwinBinding::focusWindow(quint32 handle, const QString &seat) {
    if (!shell_) return;
    QByteArray seatUtf8 = seat.toUtf8();
    qdwin_shell_v1_set_keyboard_focus(shell_, seatUtf8.constData(), handle);
    flushAfterRequest(__func__);
}

void QdwinBinding::closeWindow(quint32 handle) {
    if (!shell_) return;
    qdwin_shell_v1_request_close(shell_, handle);
    flushAfterRequest(__func__);
}

void QdwinBinding::requestMaximize(quint32 handle, bool maximized) {
    if (!shell_) return;
    qdwin_shell_v1_request_maximize(shell_, handle, maximized ? 1u : 0u);
    flushAfterRequest(__func__);
}

void QdwinBinding::requestMinimize(quint32 handle) {
    if (!shell_) return;
    qdwin_shell_v1_request_minimize(shell_, handle);
    flushAfterRequest(__func__);
}

void QdwinBinding::setBorderColor(quint32 handle, quint32 argb) {
    if (!shell_) return;
    qdwin_shell_v1_set_border_color(shell_, handle, argb);
    flushAfterRequest(__func__);
}

// -------- v24 workspaces (ext-workspace-v1 client) --------

void QdwinBinding::wsBindGroup(ext_workspace_group_handle_v1 *grp) {
    wsGroup_ = grp;
    ext_workspace_group_handle_v1_add_listener(grp, &kWsGroupListener, this);
}

void QdwinBinding::wsBindHandle(ext_workspace_handle_v1 *ws) {
    WsEntry e;
    e.proxy = ws;
    wsEntries_.push_back(e);
    ext_workspace_handle_v1_add_listener(ws, &kWsHandleListener, this);
}

QdwinBinding::WsEntry *QdwinBinding::wsEntryFor(ext_workspace_handle_v1 *h) {
    for (auto &e : wsEntries_)
        if (e.proxy == h)
            return &e;
    return nullptr;
}

// Collapse the accumulated handle events (fired since the last `done`)
// into the index-ordered view the bar consumes. Drops removed entries,
// orders by the 1-D coordinate qdwin sends (falls back to arrival order),
// and recomputes count + active. Emits workspacesChanged only on a real
// change so QML rebinds aren't spammed by no-op state echoes.
void QdwinBinding::wsRebuild() {
    // Reap removed handles (the compositor sent `removed`; the proxy is
    // inert — destroy it and forget the entry).
    for (auto it = wsEntries_.begin(); it != wsEntries_.end();) {
        if (it->removed) {
            if (it->proxy)
                ext_workspace_handle_v1_destroy(it->proxy);
            it = wsEntries_.erase(it);
        } else {
            ++it;
        }
    }
    // Order by coordinate so wsByIndex_[i] is workspace i.
    std::vector<WsEntry *> ordered;
    ordered.reserve(wsEntries_.size());
    for (auto &e : wsEntries_)
        ordered.push_back(&e);
    std::stable_sort(ordered.begin(), ordered.end(),
                     [](const WsEntry *a, const WsEntry *b) {
                         if (a->haveCoord && b->haveCoord)
                             return a->coord < b->coord;
                         return false;  // keep arrival order otherwise
                     });

    std::vector<ext_workspace_handle_v1 *> byIndex;
    quint32 active = 0;
    constexpr uint32_t kActive = 1u;  // EXT_WORKSPACE_HANDLE_V1_STATE_ACTIVE
    byIndex.reserve(ordered.size());
    for (auto *e : ordered) {
        if (e->state & kActive)
            active = static_cast<quint32>(byIndex.size());
        byIndex.push_back(e->proxy);
    }

    const quint32 count = static_cast<quint32>(byIndex.size());
    const bool changed = (count != workspaceCount_) ||
                         (active != activeWorkspace_) ||
                         (byIndex != wsByIndex_);
    wsByIndex_ = std::move(byIndex);
    workspaceCount_ = count;
    activeWorkspace_ = active;
    if (changed)
        emit workspacesChanged();
}

void QdwinBinding::wsTeardownState() {
    // Disconnect path: the wl_display is already gone (teardown()
    // disconnects before calling us), so the proxies are reaped with it.
    // Just drop our view so a fresh bind starts clean — do NOT touch the
    // dead proxies.
    wsEntries_.clear();
    wsByIndex_.clear();
    wsManager_ = nullptr;
    wsGroup_ = nullptr;
    if (workspaceCount_ != 0 || activeWorkspace_ != 0) {
        workspaceCount_ = 0;
        activeWorkspace_ = 0;
        emit workspacesChanged();
    }
}

// manager.finished path: the display is still live, so we own and must
// release the workspace/group proxies. (We never call ext_workspace_
// manager_v1.stop ourselves, so in practice this only fires if the
// compositor tears the manager down on its own.) The manager interface
// has no destroy request; we drop our reference and the proxy is reaped
// on the next disconnect.
void QdwinBinding::wsFinished() {
    for (auto &e : wsEntries_)
        if (e.proxy)
            ext_workspace_handle_v1_destroy(e.proxy);
    if (wsGroup_)
        ext_workspace_group_handle_v1_destroy(wsGroup_);
    wsTeardownState();
}

void QdwinBinding::activateWorkspace(quint32 index) {
    if (!wsManager_ || index >= wsByIndex_.size())
        return;
    ext_workspace_handle_v1_activate(wsByIndex_[index]);
    ext_workspace_manager_v1_commit(wsManager_);
    flushAfterRequest(__func__);
}

void QdwinBinding::createWorkspace() {
    if (!wsManager_ || !wsGroup_)
        return;
    // Name is positional on the qdwin side (ignored); the user's display
    // name is a shell-side overlay. Pass empty.
    ext_workspace_group_handle_v1_create_workspace(wsGroup_, "");
    ext_workspace_manager_v1_commit(wsManager_);
    flushAfterRequest(__func__);
}

void QdwinBinding::removeWorkspace(quint32 index) {
    if (!wsManager_ || index >= wsByIndex_.size())
        return;
    ext_workspace_handle_v1_remove(wsByIndex_[index]);
    ext_workspace_manager_v1_commit(wsManager_);
    flushAfterRequest(__func__);
}

// Reconcile the compositor's workspace count to the shell's persisted
// setting by appending / removing from the end, then commit once. The
// model updates asynchronously via the manager `done`(s) that follow.
void QdwinBinding::setWorkspaceCount(quint32 count) {
    if (!wsManager_ || !wsGroup_)
        return;
    if (count < 1) count = 1;
    if (count > 32) count = 32;
    const quint32 cur = static_cast<quint32>(wsByIndex_.size());
    if (count > cur) {
        for (quint32 i = cur; i < count; i++)
            ext_workspace_group_handle_v1_create_workspace(wsGroup_, "");
    } else if (count < cur) {
        // Remove the highest-index workspaces first.
        for (quint32 i = cur; i > count; i--)
            ext_workspace_handle_v1_remove(wsByIndex_[i - 1]);
    } else {
        return;  // already matches
    }
    ext_workspace_manager_v1_commit(wsManager_);
    flushAfterRequest(__func__);
}

void QdwinBinding::moveToplevelToWorkspace(quint32 handle, quint32 index) {
    if (!shell_ || shellVersion_ < 24)
        return;
    qdwin_shell_v1_move_toplevel_to_workspace(shell_, handle, index);
    flushAfterRequest(__func__);
}

// ==================== v25 window-manager policy ====================

void QdwinBinding::setWmPolicy(quint32 focusPolicy, quint32 ffmDelayMs,
                               bool raiseOnClick, bool raiseOnHover,
                               quint32 placement, bool snapEnabled,
                               quint32 snapDistance) {
    if (!shell_ || shellVersion_ < 25)
        return;
    qdwin_shell_v1_set_wm_policy(shell_, focusPolicy, ffmDelayMs,
                                 raiseOnClick ? 1u : 0u,
                                 raiseOnHover ? 1u : 0u,
                                 placement, snapEnabled ? 1u : 0u,
                                 snapDistance);
    flushAfterRequest(__func__);
}

void QdwinBinding::requestFullscreen(quint32 handle, bool fullscreen) {
    if (!shell_ || shellVersion_ < 25)
        return;
    qdwin_shell_v1_request_fullscreen(shell_, handle, fullscreen ? 1u : 0u);
    flushAfterRequest(__func__);
}

void QdwinBinding::requestTile(quint32 handle, quint32 tileEdge) {
    if (!shell_ || shellVersion_ < 25)
        return;
    qdwin_shell_v1_request_tile(shell_, handle, tileEdge);
    flushAfterRequest(__func__);
}

void QdwinBinding::requestSetPosition(quint32 handle, qint32 x, qint32 y) {
    if (!shell_ || shellVersion_ < 30)
        return;
    qdwin_shell_v1_request_set_position(shell_, handle, x, y);
    flushAfterRequest(__func__);
}

void QdwinBinding::setRemoteOutputInput(const QString &outputName,
                                        bool enabled) {
    if (!shell_ || shellVersion_ < 34 ||
        !QRegularExpression(QStringLiteral("^rdp-[0-9]{1,3}$"))
             .match(outputName).hasMatch())
        return;
    const QByteArray encoded = outputName.toUtf8();
    qdwin_shell_v1_set_remote_output_input(
        shell_, encoded.constData(), enabled ? 1u : 0u);
    flushAfterRequest(__func__);
}

void QdwinBinding::drainRemoteOutputState(const QString &outputName) {
    if (!shell_ || shellVersion_ < 34 ||
        !QRegularExpression(QStringLiteral("^rdp-[0-9]{1,3}$"))
             .match(outputName).hasMatch())
        return;
    const QByteArray encoded = outputName.toUtf8();
    qdwin_shell_v1_drain_remote_output_state(shell_, encoded.constData());
    flushAfterRequest(__func__);
}

void QdwinBinding::registerHotkey(quint32 id, quint32 modifiers, quint32 key) {
    // register_hotkey is a v19 request but was never wired; gate at our
    // current bind version so it only fires when the compositor supports it.
    if (!shell_ || shellVersion_ < 19)
        return;
    qdwin_shell_v1_register_hotkey(shell_, id, modifiers, key);
    flushAfterRequest(__func__);
}

void QdwinBinding::unregisterHotkey(quint32 id) {
    if (!shell_ || shellVersion_ < 19)
        return;
    qdwin_shell_v1_unregister_hotkey(shell_, id);
    flushAfterRequest(__func__);
}

// ==================== v26 idle / DPMS ====================

void QdwinBinding::setIdleNotification(quint32 slot, quint32 timeoutMs) {
    if (slot >= static_cast<quint32>(kIdleSlots))
        return;
    IdleSlot &s = idleSlots_[slot];
    // Always drop any prior notification for this slot first (timeout edit /
    // disarm) so we never leak or double-fire.
    if (s.notif) {
        ext_idle_notification_v1_destroy(s.notif);
        s.notif = nullptr;
    }
    if (timeoutMs == 0 || !idleNotifier_ || !seat_) {
        flushAfterRequest(__func__);
        return;
    }
    s.self = this;
    s.slot = slot;
    s.notif = ext_idle_notifier_v1_get_idle_notification(
        idleNotifier_, timeoutMs, seat_);
    if (s.notif)
        ext_idle_notification_v1_add_listener(s.notif, &kIdleListener, &s);
    flushAfterRequest(__func__);
}

void QdwinBinding::setDisplayPower(bool on) {
    if (!shell_ || shellVersion_ < 26)
        return;
    qdwin_shell_v1_set_display_power(shell_, on ? 1u : 0u);
    flushAfterRequest(__func__);
}

// v27 ext-workspace-v1 NAME parity: forward the user's custom workspace
// name so qdwin echoes it on the standard ext_workspace_handle_v1.name
// event to every ext-workspace client. Capability-gated on the negotiated
// qdwin_shell_v1 version (>= 27); a no-op against an older compositor, so a
// third-party bar simply falls back to positional names. An empty name
// reverts that workspace to its positional default.
void QdwinBinding::setWorkspaceName(int index, const QString &name) {
    if (!shell_ || shellVersion_ < 27)
        return;
    if (index < 0)
        return;
    qdwin_shell_v1_set_workspace_name(shell_,
        static_cast<uint32_t>(index), name.toUtf8().constData());
    flushAfterRequest(__func__);
}

// ==================== v28 live input config ====================

// Push the full libinput pointer/touchpad snapshot. Capability-gated on a
// >= v28 bind; a no-op against an older compositor, so the Mouse tab simply
// stays persist-only. The compositor clamps accelSpeed and normalises the
// accelProfile / scrollMethod enums server-side, but we forward bools as a
// clean 0/1 so the wire carries canonical values.
void QdwinBinding::setPointerConfig(int accelSpeed, quint32 accelProfile,
                                    bool naturalScroll, bool tapToClick,
                                    bool leftHanded, bool middleEmulation,
                                    bool disableWhileTyping,
                                    quint32 scrollMethod) {
    if (!shell_ || shellVersion_ < 28)
        return;
    qdwin_shell_v1_set_pointer_config(shell_,
        static_cast<int32_t>(accelSpeed), accelProfile,
        naturalScroll ? 1u : 0u, tapToClick ? 1u : 0u,
        leftHanded ? 1u : 0u, middleEmulation ? 1u : 0u,
        disableWhileTyping ? 1u : 0u, scrollMethod);
    flushAfterRequest(__func__);
}

void QdwinBinding::setKeyRepeat(quint32 rate, quint32 delay) {
    if (!shell_ || shellVersion_ < 28)
        return;
    qdwin_shell_v1_set_key_repeat(shell_, rate, delay);
    flushAfterRequest(__func__);
}

void QdwinBinding::idleGlobalRemoved(uint32_t name) {
    // Wayland registry hot-remove (display still valid, so we DO destroy the
    // live proxies — unlike idleTeardownState's disconnect path). If the seat
    // or the notifier goes away the idle capability is gone: cancel every live
    // notification and drop both proxies so a later setIdleNotification can't
    // touch a stale global. (Single-seat qdwin doesn't do this today, but the
    // protocol permits it.)
    if (name != seatName_ && name != idleNotifierName_)
        return;
    for (int i = 0; i < kIdleSlots; ++i) {
        if (idleSlots_[i].notif) {
            ext_idle_notification_v1_destroy(idleSlots_[i].notif);
            idleSlots_[i].notif = nullptr;
        }
    }
    if (name == idleNotifierName_ && idleNotifier_) {
        ext_idle_notifier_v1_destroy(idleNotifier_);
        idleNotifier_ = nullptr;
        idleNotifierName_ = 0;
    }
    if (name == seatName_ && seat_) {
        wl_seat_destroy(seat_);
        seat_ = nullptr;
        seatName_ = 0;
    }
    flushAfterRequest(__func__);
    emit idleNotifierAvailableChanged();
}

void QdwinBinding::idleTeardownState() {
    // Disconnect path: the wl proxies are reaped with the display (same as
    // omTeardownState), so DON'T wl_*_destroy here — just drop our view so a
    // fresh bind re-arms cleanly. (Live re-arm/disarm in setIdleNotification
    // destroys proxies explicitly while the display is alive.)
    for (int i = 0; i < kIdleSlots; ++i) {
        idleSlots_[i].notif = nullptr;
        idleSlots_[i].self = nullptr;
    }
    idleNotifier_ = nullptr;
    idleNotifierName_ = 0;
    seat_ = nullptr;
    seatName_ = 0;
    emit idleNotifierAvailableChanged();
}

// ==================== output (display) management ====================

void QdwinBinding::omBindManager(zwlr_output_manager_v1 *mgr) {
    omManager_ = mgr;
    zwlr_output_manager_v1_add_listener(mgr, &kOmManagerListener, this);
}

void QdwinBinding::omBindHead(zwlr_output_head_v1 *head) {
    OmHeadInfo h;
    h.proxy = head;
    omHeads_.push_back(std::move(h));
    zwlr_output_head_v1_add_listener(head, &kOmHeadListener, this);
}

QdwinBinding::OmHeadInfo *QdwinBinding::omHeadFor(zwlr_output_head_v1 *h) {
    for (auto &e : omHeads_)
        if (e.proxy == h)
            return &e;
    return nullptr;
}

QdwinBinding::OmModeInfo *QdwinBinding::omModeFor(zwlr_output_mode_v1 *m) {
    for (auto &h : omHeads_)
        for (auto &md : h.modes)
            if (md.proxy == m)
                return &md;
    return nullptr;
}

// Collapse the accumulated head/mode events into the QVariantList the
// Display layout tab renders. The protocol re-sends the whole head set on
// every `done` (after destroying the old heads with `finished`), so we
// rebuild from scratch each time and forget the stale proxies — they are
// inert. Each output map carries name/description/make/model/serial (all
// PlainText on the QML side — never shell-interpolated), enabled, x/y,
// scale, transform, the mode list, and the current mode index.
void QdwinBinding::omRebuild() {
    // Reap heads the compositor has finished (it destroys + recreates the
    // whole head set on every resync). Their proxies were already released in
    // hd_finished; drop the entries so outputs_ and omSubmitLayout only ever
    // see live heads.
    omHeads_.erase(std::remove_if(omHeads_.begin(), omHeads_.end(),
                   [](const OmHeadInfo &h) { return h.finished; }),
                   omHeads_.end());
    QVariantList out;
    for (const auto &h : omHeads_) {
        QVariantMap m;
        m["name"] = h.name;
        m["description"] = h.description;
        m["make"] = h.make;
        m["model"] = h.model;
        m["serial"] = h.serial;
        m["enabled"] = h.enabled;
        m["x"] = h.x;
        m["y"] = h.y;
        m["scale"] = h.scale;
        m["transform"] = h.transform;
        QVariantList modes;
        int currentIdx = -1;
        for (int i = 0; i < static_cast<int>(h.modes.size()); ++i) {
            const auto &md = h.modes[i];
            QVariantMap mm;
            mm["width"] = md.width;
            mm["height"] = md.height;
            mm["refresh"] = md.refresh;
            mm["preferred"] = md.preferred;
            modes.append(mm);
            if (md.proxy == h.currentMode)
                currentIdx = i;
        }
        m["modes"] = modes;
        m["currentMode"] = currentIdx;
        out.append(m);
    }
    outputs_ = std::move(out);
    emit outputsChanged();
}

void QdwinBinding::omTeardownState() {
    // Disconnect / manager.finished path: proxies are reaped with the
    // display (or inert after finished). Drop our view so a fresh bind
    // starts clean.
    omHeads_.clear();
    omConfigs_.clear();
    outputs_.clear();
    omManager_ = nullptr;
    outputSerial_ = 0;
    emit outputsChanged();
}

void QdwinBinding::omConfigResult(zwlr_output_configuration_v1 *cfg, bool ok,
                                  bool cancelled) {
    bool applied = false;
    QString tag;
    for (auto it = omConfigs_.begin(); it != omConfigs_.end(); ++it) {
        if (it->proxy == cfg) {
            applied = it->applied;
            tag = it->tag;
            omConfigs_.erase(it);
            break;
        }
    }
    // Per spec the client destroys the configuration object on any of
    // succeeded/failed/cancelled.
    zwlr_output_configuration_v1_destroy(cfg);
    flushAfterRequest(__func__);
    if (!tag.isEmpty())
        emit layoutTaggedResult(tag, ok, cancelled);
    else
        emit layoutResult(applied, ok, cancelled);
}

// Build a configuration for `layout` against `serial` and apply or test it.
// Returns false (no attempt) if there is no live manager. Every advertised
// head must be configured (the protocol errors on an omitted head), so we
// iterate the enumerated head set and either match it to a layout entry
// (by name) or carry its current enabled state forward unchanged.
bool QdwinBinding::omSubmitLayout(const QVariantList &layout, quint32 serial,
                                  bool apply, const QString &tag) {
    if (!omManager_)
        return false;

    auto *cfg = zwlr_output_manager_v1_create_configuration(omManager_, serial);
    if (!cfg)
        return false;
    OmConfig rec;
    rec.proxy = cfg;
    rec.applied = apply;
    rec.tag = tag;
    omConfigs_.push_back(rec);
    zwlr_output_configuration_v1_add_listener(cfg, &kOmConfigListener, this);

    for (auto &h : omHeads_) {
        if (h.finished || !h.proxy)
            continue;  // inert head — never reference a dead proxy
        // Find the matching layout entry by name (PlainText match; names
        // come from the compositor, not user input).
        const QVariantMap *want = nullptr;
        QVariantMap wantStore;
        for (const QVariant &v : layout) {
            QVariantMap e = v.toMap();
            if (e.value("name").toString() == h.name) {
                wantStore = e;
                want = &wantStore;
                break;
            }
        }
        bool enable = want ? want->value("enabled", h.enabled).toBool()
                           : h.enabled;
        if (!enable) {
            zwlr_output_configuration_v1_disable_head(cfg, h.proxy);
            continue;
        }
        auto *ch = zwlr_output_configuration_v1_enable_head(cfg, h.proxy);
        if (!want)
            continue;  // enabled, untouched — keep all current properties
        // Mode: prefer an exact width/height/refresh match against an
        // advertised mode (set_mode); fall back to set_custom_mode so the
        // compositor can validate against its mode_list.
        if (want->contains("width") && want->contains("height")) {
            int w = want->value("width").toInt();
            int hh = want->value("height").toInt();
            int refresh = want->value("refresh", 0).toInt();
            zwlr_output_mode_v1 *exact = nullptr;
            for (const auto &md : h.modes) {
                if (md.width == w && md.height == hh &&
                    (refresh == 0 || md.refresh == refresh)) {
                    exact = md.proxy;
                    break;
                }
            }
            if (exact)
                zwlr_output_configuration_head_v1_set_mode(ch, exact);
            else
                zwlr_output_configuration_head_v1_set_custom_mode(ch, w, hh,
                                                                  refresh);
        }
        if (want->contains("x") && want->contains("y"))
            zwlr_output_configuration_head_v1_set_position(ch,
                want->value("x").toInt(), want->value("y").toInt());
        if (want->contains("transform"))
            zwlr_output_configuration_head_v1_set_transform(ch,
                want->value("transform").toInt());
        if (want->contains("scale")) {
            double sc = want->value("scale").toDouble();
            if (sc <= 0) sc = 1.0;
            zwlr_output_configuration_head_v1_set_scale(ch,
                wl_fixed_from_double(sc));
        }
    }

    if (apply)
        zwlr_output_configuration_v1_apply(cfg);
    else
        zwlr_output_configuration_v1_test(cfg);
    flushAfterRequest(__func__);
    return true;
}

bool QdwinBinding::applyLayout(const QVariantList &layout, quint32 serial) {
    return omSubmitLayout(layout, serial, true);
}

bool QdwinBinding::applyLayoutTagged(const QVariantList &layout, quint32 serial,
                                     const QString &tag) {
    if (tag.isEmpty() || tag.size() > 128)
        return false;
    return omSubmitLayout(layout, serial, true, tag);
}

bool QdwinBinding::testLayout(const QVariantList &layout, quint32 serial) {
    return omSubmitLayout(layout, serial, false);
}

// spec/10 §"clear_selection" — deny verdict from broker; compositor
// drops the seat's selection (and primary equivalent when isPrimary=1).
void QdwinBinding::clearSelection(const QString &seat, quint32 isPrimary) {
    if (!shell_) return;
    // F5: strip control chars so an embedded NUL can't truncate the seat name.
    QByteArray seatUtf8 = stripControlChars(seat).toUtf8();
    qdwin_shell_v1_clear_selection(shell_, seatUtf8.constData(), isPrimary);
    flushAfterRequest(__func__);
}

// spec/10 §"receive-time gating" — echo the broker verdict back for a
// pending wl_data_offer.receive. "allow" runs the source's original
// send; anything else (incl. our "deny") closes the destination fd.
void QdwinBinding::sendDataOfferReceiveDecision(quint32 requestHandle,
                                                bool allow) {
    if (!shell_) return;
    qdwin_shell_v1_data_offer_receive_decision(shell_, requestHandle,
                                               allow ? "allow" : "deny");
    flushAfterRequest(__func__);
}

void QdwinBinding::nestedProxyDecision(quint32 handle, quint32 decision,
                                       const QString &reason) {
    if (!shell_) return;
    // F5: outbound reason is informational; strip control chars before the ABI.
    QByteArray reasonUtf8 = stripControlChars(reason).toUtf8();
    qdwin_shell_v1_nested_proxy_decision(shell_, handle, decision,
                                         reasonUtf8.constData());
    flushAfterRequest(__func__);
}

void QdwinBinding::activationDecision(quint32 handle, quint32 decision,
                                      const QString &reason) {
    if (!shell_) return;
    // F5: outbound reason is informational; strip control chars before the ABI.
    QByteArray reasonUtf8 = stripControlChars(reason).toUtf8();
    qdwin_shell_v1_activation_decision(shell_, handle, decision,
                                       reasonUtf8.constData());
    flushAfterRequest(__func__);
}

QVariantMap QdwinBinding::checkPermission(const QString &action,
                                          const QVariantMap &details) {
    QStringList args = {
        QStringLiteral("--system"),
        QStringLiteral("--no-pager"),
        QString::fromLatin1(kBrokerGateBusctlTimeout),
        QStringLiteral("call"),
        QStringLiteral("org.qdistro.AdminBroker1"),
        QStringLiteral("/org/qdistro/AdminBroker1"),
        QStringLiteral("org.qdistro.AdminBroker1"),
        QStringLiteral("CheckPermission"),
        QStringLiteral("sa{sv}"),
        action,
    };
    appendVariantDict(args, details);

    QProcess proc;
    proc.setProgram(QStringLiteral("busctl"));
    proc.setArguments(args);
    proc.start();
    if (!proc.waitForStarted(kBrokerStartTimeoutMs)) {
        return {
            {QStringLiteral("exitCode"), -1},
            {QStringLiteral("stdout"), QString()},
            {QStringLiteral("stderr"), proc.errorString()},
            {QStringLiteral("timedOut"), false},
        };
    }
    if (!proc.waitForFinished(kBrokerGateTimeoutMs)) {
        proc.kill();
        proc.waitForFinished(50);
        return {
            {QStringLiteral("exitCode"), -1},
            {QStringLiteral("stdout"),
             QString::fromUtf8(proc.readAllStandardOutput())},
            {QStringLiteral("stderr"), QStringLiteral("timeout")},
            {QStringLiteral("timedOut"), true},
        };
    }
    return {
        {QStringLiteral("exitCode"), proc.exitCode()},
        {QStringLiteral("stdout"),
         QString::fromUtf8(proc.readAllStandardOutput())},
        {QStringLiteral("stderr"),
         QString::fromUtf8(proc.readAllStandardError())},
        {QStringLiteral("timedOut"), false},
    };
}

bool QdwinBinding::verifyClientIdentity(
    quint32 pid,
    quint64 starttime,
    quint32 uid,
    const QString &exe,
    const QString &selinuxLabel,
    const QString &sandboxEngine,
    const QString &appId,
    const QString &instanceId) {
    // F5: reject fail-closed if any identity string carries a control char.
    if (anyControlChars({&exe, &selinuxLabel, &sandboxEngine, &appId,
                         &instanceId}))
        return false;
    QStringList args = {
        QStringLiteral("--system"),
        QStringLiteral("--no-pager"),
        QString::fromLatin1(kBrokerGateBusctlTimeout),
        QStringLiteral("call"),
        QStringLiteral("org.qdistro.AdminBroker1"),
        QStringLiteral("/org/qdistro/AdminBroker1"),
        QStringLiteral("org.qdistro.AdminBroker1"),
        QStringLiteral("VerifyClientIdentity"),
        QStringLiteral("utusssss"),
        QString::number(pid),
        QString::number(starttime),
        QString::number(uid),
        exe,
        selinuxLabel,
        sandboxEngine,
        appId,
        instanceId,
    };

    QProcess proc;
    proc.setProgram(QStringLiteral("busctl"));
    proc.setArguments(args);
    proc.start();
    if (!proc.waitForStarted(kBrokerStartTimeoutMs))
        return false;
    if (!proc.waitForFinished(kBrokerGateTimeoutMs)) {
        proc.kill();
        proc.waitForFinished(50);
        return false;
    }
    if (proc.exitCode() != 0)
        return false;
    const QString out = QString::fromUtf8(proc.readAllStandardOutput()).trimmed();
    return out == QStringLiteral("b true");
}

QVariantMap QdwinBinding::checkHandoffActivation(
    const QString &sourceSilo,
    const QString &destSilo,
    const QString &sourceAppId,
    const QString &destAppId,
    const QString &sourceSandboxEngine,
    bool identityVerified,
    uint sourcePid,
    qulonglong sourceStarttime) {
    // F5: fail closed on any control char in the relayed identity strings.
    if (anyControlChars({&sourceSilo, &destSilo, &sourceAppId, &destAppId,
                         &sourceSandboxEngine}))
        return rejectedIdentityResult();
    QStringList args = {
        QStringLiteral("--system"),
        QStringLiteral("--no-pager"),
        QString::fromLatin1(kBrokerGateBusctlTimeout),
        QStringLiteral("call"),
        QStringLiteral("org.qdistro.AdminBroker1"),
        QStringLiteral("/org/qdistro/AdminBroker1"),
        QStringLiteral("org.qdistro.AdminBroker1"),
        QStringLiteral("CheckHandoffActivation"),
        QStringLiteral("sssssbut"),
        sourceSilo,
        destSilo,
        sourceAppId,
        destAppId,
        sourceSandboxEngine,
        identityVerified ? QStringLiteral("true") : QStringLiteral("false"),
        QString::number(sourcePid),
        QString::number(sourceStarttime),
    };

    QProcess proc;
    proc.setProgram(QStringLiteral("busctl"));
    proc.setArguments(args);
    proc.start();
    if (!proc.waitForStarted(kBrokerStartTimeoutMs)) {
        return {
            {QStringLiteral("exitCode"), -1},
            {QStringLiteral("stdout"), QString()},
            {QStringLiteral("stderr"), proc.errorString()},
            {QStringLiteral("timedOut"), false},
        };
    }
    if (!proc.waitForFinished(kBrokerGateTimeoutMs)) {
        proc.kill();
        proc.waitForFinished(50);
        return {
            {QStringLiteral("exitCode"), -1},
            {QStringLiteral("stdout"),
             QString::fromUtf8(proc.readAllStandardOutput())},
            {QStringLiteral("stderr"), QStringLiteral("timeout")},
            {QStringLiteral("timedOut"), true},
        };
    }
    return {
        {QStringLiteral("exitCode"), proc.exitCode()},
        {QStringLiteral("stdout"),
         QString::fromUtf8(proc.readAllStandardOutput())},
        {QStringLiteral("stderr"),
         QString::fromUtf8(proc.readAllStandardError())},
        {QStringLiteral("timedOut"), false},
    };
}

QVariantMap QdwinBinding::checkClipboardTransfer(
    const QString &sourceSilo,
    const QString &destSilo,
    const QStringList &mimeTypes,
    const QString &sourceAppId,
    const QString &destAppId,
    const QString &sourceSandboxEngine,
    bool identityVerified,
    uint sourcePid,
    qulonglong sourceStarttime) {
    // F5: fail closed on any control char in the identity strings or any mime
    // entry (every mimeTypes item also becomes a busctl arg).
    if (anyControlChars({&sourceSilo, &destSilo, &sourceAppId, &destAppId,
                         &sourceSandboxEngine}))
        return rejectedIdentityResult();
    for (const QString &mime : mimeTypes)
        if (hasControlChars(mime))
            return rejectedIdentityResult();
    QStringList args = {
        QStringLiteral("--system"),
        QStringLiteral("--no-pager"),
        QString::fromLatin1(kBrokerDefaultBusctlTimeout),
        QStringLiteral("call"),
        QStringLiteral("org.qdistro.AdminBroker1"),
        QStringLiteral("/org/qdistro/AdminBroker1"),
        QStringLiteral("org.qdistro.AdminBroker1"),
        QStringLiteral("CheckClipboardTransfer"),
        QStringLiteral("ssassssbut"),
        sourceSilo,
        destSilo,
        QString::number(mimeTypes.size()),
    };
    args.append(mimeTypes);
    args.append(sourceAppId);
    args.append(destAppId);
    args.append(sourceSandboxEngine);
    args.append(identityVerified ? QStringLiteral("true")
                                 : QStringLiteral("false"));
    args.append(QString::number(sourcePid));
    args.append(QString::number(sourceStarttime));

    QProcess proc;
    proc.setProgram(QStringLiteral("busctl"));
    proc.setArguments(args);
    proc.start();
    if (!proc.waitForStarted(kBrokerStartTimeoutMs)) {
        return {
            {QStringLiteral("exitCode"), -1},
            {QStringLiteral("stdout"), QString()},
            {QStringLiteral("stderr"), proc.errorString()},
            {QStringLiteral("timedOut"), false},
        };
    }
    if (!proc.waitForFinished(kBrokerDefaultTimeoutMs)) {
        proc.kill();
        proc.waitForFinished(50);
        return {
            {QStringLiteral("exitCode"), -1},
            {QStringLiteral("stdout"),
             QString::fromUtf8(proc.readAllStandardOutput())},
            {QStringLiteral("stderr"), QStringLiteral("timeout")},
            {QStringLiteral("timedOut"), true},
        };
    }
    return {
        {QStringLiteral("exitCode"), proc.exitCode()},
        {QStringLiteral("stdout"),
         QString::fromUtf8(proc.readAllStandardOutput())},
        {QStringLiteral("stderr"),
         QString::fromUtf8(proc.readAllStandardError())},
        {QStringLiteral("timedOut"), false},
    };
}

// spec/10 receive-time twin of checkClipboardTransfer. Signature
// ssssssb with a SINGLE mime (the compositor gates each receive()
// individually, so there is no count/list as at set time).
QVariantMap QdwinBinding::checkClipboardReceive(
    const QString &sourceSilo,
    const QString &destSilo,
    const QString &mimeType,
    const QString &sourceAppId,
    const QString &destAppId,
    const QString &sourceSandboxEngine,
    bool identityVerified,
    uint sourcePid,
    qulonglong sourceStarttime) {
    // F5: fail closed on any control char in the identity strings or mime type.
    if (anyControlChars({&sourceSilo, &destSilo, &mimeType, &sourceAppId,
                         &destAppId, &sourceSandboxEngine}))
        return rejectedIdentityResult();
    QStringList args = {
        QStringLiteral("--system"),
        QStringLiteral("--no-pager"),
        QString::fromLatin1(kBrokerDefaultBusctlTimeout),
        QStringLiteral("call"),
        QStringLiteral("org.qdistro.AdminBroker1"),
        QStringLiteral("/org/qdistro/AdminBroker1"),
        QStringLiteral("org.qdistro.AdminBroker1"),
        QStringLiteral("CheckClipboardReceive"),
        QStringLiteral("ssssssbut"),
        sourceSilo,
        destSilo,
        mimeType,
        sourceAppId,
        destAppId,
        sourceSandboxEngine,
        identityVerified ? QStringLiteral("true")
                         : QStringLiteral("false"),
        QString::number(sourcePid),
        QString::number(sourceStarttime),
    };

    QProcess proc;
    proc.setProgram(QStringLiteral("busctl"));
    proc.setArguments(args);
    proc.start();
    if (!proc.waitForStarted(kBrokerStartTimeoutMs)) {
        return {
            {QStringLiteral("exitCode"), -1},
            {QStringLiteral("stdout"), QString()},
            {QStringLiteral("stderr"), proc.errorString()},
            {QStringLiteral("timedOut"), false},
        };
    }
    if (!proc.waitForFinished(kBrokerDefaultTimeoutMs)) {
        proc.kill();
        proc.waitForFinished(50);
        return {
            {QStringLiteral("exitCode"), -1},
            {QStringLiteral("stdout"),
             QString::fromUtf8(proc.readAllStandardOutput())},
            {QStringLiteral("stderr"), QStringLiteral("timeout")},
            {QStringLiteral("timedOut"), true},
        };
    }
    return {
        {QStringLiteral("exitCode"), proc.exitCode()},
        {QStringLiteral("stdout"),
         QString::fromUtf8(proc.readAllStandardOutput())},
        {QStringLiteral("stderr"),
         QString::fromUtf8(proc.readAllStandardError())},
        {QStringLiteral("timedOut"), false},
    };
}
