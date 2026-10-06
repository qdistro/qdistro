pragma Singleton
import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "ClipboardBroker.js" as ClipboardBroker
import "ClipboardSilo.js" as ClipboardSilo
import "ClipboardFocusClear.js" as ClipboardFocusClear
import "ClipboardDenyCoalesce.js" as ClipboardDenyCoalesce

// spec/10 Phase-1 — compositor-mediated clipboard gate.
// Track-04 Phase-1 scope. Implements the cross-silo clipboard
// protection that pairs with qdwin's `selection_set` and
// `toplevel_security_context` events (qdwin_shell_v1 v13+).
// Lifecycle:
//   - Qdwin.qml's QdwinBinding emits `toplevelSecurityContext(handle,
//     sandboxEngine, appId, instanceId)` shortly after each
//     `toplevelAdded`. We build a handle → silo map from these events.
//   - On `selectionSet(seat, sourceHandle, mimeTypesConcat, isPrimary)`,
//     we look up the source silo from the map, the destination silo
//     from the currently-focused toplevel, and decide allow/deny
//     by asking the qdistro broker's CheckClipboardTransfer method
//     (asynchronously: the verdict lands when the broker answers, up to 2 s
//     later; a newer selection makes an older verdict non-clearing).
//   - On deny, we call `QdwinBinding.clearSelection(seat, isPrimary)`.
// Decision audit: every verdict emits a journal line of the form
//   CLIPBOARD_GATE seat=<s> src_silo=<s> dst_silo=<s> mime_types=<csv>
//                  verdict=<allow|deny> reason=<text>
// (the qdistro VM test harness asserts on these — the line shape is
// stable and any field re-ordering is a breaking change).
// Broker failures are fail-closed: absent broker, timeout, malformed
// reply, and unknown verdict all deny and clear. Unknown source or
// destination identity is denied locally before broker evaluation
// because there is no trustworthy action key for rules/cache lookup.
// ClipboardPolicy.qml is still loaded for settings/probe compatibility
// but is intentionally not used as a fallback for live enforcement.
// Focus-aware-clear (track-04 Phase-2, clipboard.md §"focus-aware-clear").
// IMPLEMENTED: _onSelectionSet records the silo that set the active
// selection (per kind: regular + primary) in _selectionSourceSilo. On
// every seatFocusChanged(seat, handle) — qdwin's seat_focus_changed —
// _onSeatFocusChanged resolves the newly focused toplevel's silo and, for
// each tracked selection kind whose source silo differs from it, calls
// clearSelection(seat, isPrimary) and emits a CLIPBOARD_FOCUS_GATE journal
// verdict. Same-silo focus changes are a no-op so same-silo paste keeps
// working (work-user copy → focus work-user terminal still pastes; only
// crossing into another silo clears). Unknown source silo is never tracked
// (nothing trustworthy to clear); unknown destination differs from any
// known source and thus clears (fail-closed). This complements — does not
// replace — the set-time and receive-time gates below.
// Receive-time gate (qdwin_shell_v1 v15+ `data_offer_receive_pending`).
// IMPLEMENTED: on every gated `wl_data_offer.receive`, qdwin blocks the
// destination fd (~2s timeout → deny) and emits dataOfferReceivePending
// (requestHandle, seat, sourceHandle, targetHandle, mimeType). Unlike
// set-time, the target is explicit (the receiving client) rather than
// the keyboard-focused toplevel, and the broker is consulted per single
// MIME via CheckClipboardReceive. _onDataOfferReceivePending resolves
// src/dst silos from the handle→silo map, applies the same tier-4 MIME
// allow-list, unknown-identity fail-closed, and Option-B identity
// verification as the set-time path, then ALWAYS answers the compositor
// exactly once via sendDataOfferReceiveDecision(requestHandle, allow) —
// every error/fallback path denies. Per-app metadata (passwordField,
// codeBlock, ...) remains a future refinement; there is no metadata
// channel yet.
Singleton {
    id: root

    // Public init — Qdwin.qml calls this after its QdwinBinding fires
    // `boundChanged → bound`, so we have a live shell handle to subscribe
    // through. Idempotent.
    function init(binding) {
        if (root._wired) {
            return;
        }
        if (!binding) {
            Logger.w("ClipboardGate", "init called with null binding");
            return;
        }
        root._binding = binding;
        binding.toplevelAdded.connect(root._onToplevelAdded);
        binding.toplevelRemoved.connect(root._onToplevelRemoved);
        binding.toplevelSecurityContext.connect(root._onSecurityContext);
        // Option-B identity sidecar (qdwin_shell_v1@v22). Older bindings
        // simply never emit; the same-silo gate then stays unverified and
        // falls through to the cross-silo policy path. See
        // todo/decisions/secctx-identity-contract.md.
        if (binding.toplevelPeerIdentity !== undefined) {
            binding.toplevelPeerIdentity.connect(root._onPeerIdentity);
        }
        binding.selectionSet.connect(root._onSelectionSet);
        // v23 sidecar — selection_set_source_identity. Fires IMMEDIATELY
        // BEFORE the matching selectionSet (qdwin guarantees the pair-by-
        // sequence ordering on the wire; the Qt direct-connect signal
        // delivery in qdwin-binding.cpp preserves it). Older bindings
        // simply never emit; src_silo then falls back to the v11
        // focus-handle path verbatim.
        if (binding.selectionSetSourceIdentity !== undefined) {
            binding.selectionSetSourceIdentity.connect(root._onSelectionSetSourceIdentity);
        }
        // v35 sidecar — selection_set_source_peer_identity. Fires
        // IMMEDIATELY AFTER the v23 sidecar for the same tagged source,
        // carrying the SOURCE wl_client's own compositor-observed peer
        // identity. Older bindings never emit; the relay then falls
        // back to the bound-handle identity path verbatim.
        if (binding.selectionSetSourcePeerIdentity !== undefined) {
            binding.selectionSetSourcePeerIdentity.connect(root._onSelectionSetSourcePeerIdentity);
        }
        // Receive-time gate (qdwin_shell_v1 v15+). Older bindings never
        // emit; cross-app paste then relies on the set-time gate +
        // focus-aware-clear, and the compositor's own ~2s deny timeout
        // for any gated receive that no shell answers.
        if (binding.dataOfferReceivePending !== undefined) {
            binding.dataOfferReceivePending.connect(root._onDataOfferReceivePending);
        }
        // focus-aware-clear (clipboard.md §"focus-aware-clear"). qdwin emits
        // seatFocusChanged(seat, handle) on every keyboard-focus transition
        // (qdwin_shell_v1 seat_focus_changed). Older bindings without the
        // signal simply never emit; the set-time gate then remains the only
        // line of defence. Phase-1 gates set-time; Phase-2 clears when focus
        // crosses out of the selection-source silo.
        if (binding.seatFocusChanged !== undefined) {
            binding.seatFocusChanged.connect(root._onSeatFocusChanged);
        }
        // Asynchronous broker checks: the set-time and receive-time gates
        // start a busctl call and finish the decision when the binding
        // reports it, so a slow broker never blocks the shell's GUI thread
        // and is given 2 s / 1.5 s instead of the synchronous 200 ms
        // (which denied legitimate same-silo pastes under load). Older
        // plugins without the async API keep the synchronous calls.
        if (binding.clipboardCheckFinished !== undefined
                && binding.startCheckClipboardTransfer !== undefined
                && binding.startCheckClipboardReceive !== undefined) {
            binding.clipboardCheckFinished.connect(root._onClipboardCheckFinished);
            root._asyncChecks = true;
        }
        binding.boundChanged.connect(() => {
            if (!binding.bound)
                root._onBindingLost();
        });
        root._wired = true;
        ClipboardPolicy.load();
        Logger.i("ClipboardGate", "wired to qdwin_shell_v1; broker default=deny");
    }

    // -- internal state -------------------------------------------------
    property bool _wired: false
    property var _binding: null

    // handle (uint32) → silo (string). Stored as a plain JS object since
    // QML ListModel doesn't support uint32 keys well.
    property var _handleToSilo: ({})
    property var _handleToAppId: ({})
    property var _handleToSandboxEngine: ({})

    // Option-B identity bookkeeping (todo/decisions/secctx-identity-contract.md):
    //   _handleToIdentity[handle] = { pid, starttime, uid, exe, label,
    //                                 sandboxEngine, appId, instanceId }
    //   _verifyCache[verifyKey]   = { verified, expires }
    //   _verifyInFlight[verifyKey] = bool  (suppress duplicate calls)
    // verifyKey covers the complete attested tuple, including PID start time.
    property var _handleToIdentity: ({})
    property var _verifyCache: ({})
    property var _verifyInFlight: ({})
    property var _verifyQueue: []
    property var _verifyActive: null
    property int _verifyGeneration: 0

    // v23 sidecar — selection_set_source_identity. The compositor fires
    // this IMMEDIATELY BEFORE the matching selectionSet for tagged
    // source clients; we stash the tuple here and consume it on the
    // very next _onSelectionSet, then clear. Pair-by-sequence: at most
    // one outstanding entry. Stale untagged-source events on a v23
    // shell skip the sidecar entirely, so _pendingSrcIdentity stays
    // null and the v11 focus-handle path takes over.
    //   { sandboxEngine, appId, instanceId }   (or null)
    property var _pendingSrcIdentity: null

    // v35 sidecar — selection_set_source_peer_identity. Fires right
    // after the v23 sidecar for the same tagged source; carries the
    // source wl_client's OWN compositor-observed peer identity. This is
    // what makes equal-tag binding unnecessary for the pid relay: the
    // identity below belongs to the very client that issued
    // set_selection, not to whichever toplevel happens to share the tag
    // tuple.
    //   { pid, starttime, uid, exe, label,
    //     sandboxEngine, appId, instanceId }   (or null)
    property var _pendingSrcPeer: null

    // focus-aware-clear bookkeeping (clipboard.md §"focus-aware-clear").
    // The silo that set the active selection, tracked per selection kind
    // ("0" = regular clipboard, "1" = primary). Recorded on every
    // _onSelectionSet (regardless of allow/deny verdict — once a source
    // owns the selection we must clear it the moment focus crosses out of
    // its silo). null = no tracked selection / source silo unknown, in
    // which case focus changes are a no-op (nothing trustworthy to clear).
    //   _selectionSourceSilo[isPrimary] = silo string (or absent)
    property var _selectionSourceSilo: ({})

    // deny-storm coalescer (clipboard.md §"deny-storm robustness",
    // ClipboardDenyCoalesce.js). Maps a denied-offer identity key (seat +
    // kind + src_silo + dst_silo + mime_csv) → the ms timestamp of the last
    // clear_selection wire call we actually issued for it. ClipboardDenyCoalesce
    // .shouldSendClear() reads + mutates this so a producer re-asserting a
    // denied selection in a tight loop can't flood the 4 KB wl output buffer
    // (which fatally errors the shell↔compositor connection). The FIRST deny
    // per key always clears; identical repeats inside _denyClearCoalesceMs are
    // suppressed at the WIRE call only — the CLIPBOARD_GATE verdict is still
    // logged every time, and fail-closed is preserved.
    property var _lastDenyClearByKey: ({})
    property int _denyClearCoalesceMs: 500

    // Asynchronous broker checks in flight, keyed by the binding's request
    // id: { kind: "transfer", entry, selKind, gen } for a set-time check,
    // { kind: "receive", requestHandle, seat, srcSilo, dstSilo, mime } for a
    // receive. _selectionGen counts selection_set events per kind ("0"
    // regular, "1" primary); a set-time verdict whose gen is no longer
    // current describes a selection that has since been replaced, so it is
    // logged but must not clear the newer selection (which gets its own
    // verdict). Paste stays gated meanwhile by the receive-time gate.
    property bool _asyncChecks: false
    property var _pendingChecks: ({})
    property var _selectionGen: ({ "0": 0, "1": 0 })

    // -- handle/silo tracking -------------------------------------------
    function _onBindingLost() {
        root._verifyGeneration++;
        root._verifyQueue = [];
        root._verifyCache = ({});
        root._verifyInFlight = ({});
        root._handleToIdentity = ({});
        root._handleToSilo = ({});
        root._handleToAppId = ({});
        root._handleToSandboxEngine = ({});
        root._selectionSourceSilo = ({});
        root._pendingSrcIdentity = null;
        root._pendingSrcPeer = null;
        root._lastDenyClearByKey = ({});
        // Replies for the lost connection's requests are ignored; the
        // compositor that issued them is gone with its receive requests.
        root._pendingChecks = ({});
    }

    function _onToplevelAdded(handle, ownerUid, appId, title, isXwayland) {
        // Until the security_context event arrives (it may, or may not —
        // qdwin only emits it for clients that bound wp_security_context_v1
        // or carry a waypipe secctx tag), we fall back to a uid-derived
        // placeholder so same-silo paste between two unctx'd toplevels in
        // the same uid still short-circuits to allow.
        if (!(handle in root._handleToSilo)) {
            root._handleToSilo[handle] = "uid:" + ownerUid;
        }
        root._handleToAppId[handle] = appId || "";
    }

    function _onToplevelRemoved(handle) {
        delete root._handleToSilo[handle];
        delete root._handleToAppId[handle];
        delete root._handleToSandboxEngine[handle];
        const identity = root._handleToIdentity[handle];
        if (identity)
            delete root._verifyCache[root._verifyKey(identity)];
        delete root._handleToIdentity[handle];
    }

    // Option-B identity sidecar from qdwin_shell_v1@v22. Caches the
    // tuple keyed by toplevel handle so the selection-set gate can find
    // it without racing the broker round-trip; the verify call itself
    // fires lazily on first use and caches by (pid, starttime).
    function _onPeerIdentity(handle, peerPid, peerStarttime, peerUid, peerExe, peerSelinuxLabel) {
        const existing = root._handleToIdentity[handle] || {};
        root._handleToIdentity[handle] = {
            "pid": peerPid >>> 0,
            "starttime": peerStarttime,
            "uid": peerUid >>> 0,
            "exe": peerExe || "",
            "label": peerSelinuxLabel || "",
            "sandboxEngine": existing.sandboxEngine || "",
            "appId": existing.appId || "",
            "instanceId": existing.instanceId || ""
        };
    }

    function _verifyKey(identity) {
        return JSON.stringify([identity.pid >>> 0, String(identity.starttime), identity.uid >>> 0, identity.exe || "", identity.label || "", identity.sandboxEngine || "", identity.appId || "", identity.instanceId || ""]);
    }

    // A Process has one active invocation: never change its request metadata
    // while it runs. Cache the complete attested tuple, with bounded retry for
    // unavailable brokers and bounded lifetime for successful attestations.
    function _ensureVerified(handle) {
        const id = root._handleToIdentity[handle];
        if (!id || !id.pid)
            return false;
        return root._queueVerify(root._verifyKey(id), handle, id);
    }

    // Same lazy broker re-verification as _ensureVerified, but for an
    // identity that is not keyed to a toplevel handle — the v35
    // selection_set_source_peer_identity sidecar. The tuple came off the
    // wire moments ago and already names the exact source wl_client, so
    // there is no handle-drift guard; the broker's /proc re-check is the
    // staleness arbiter.
    function _ensureVerifiedIdentity(id) {
        if (!id || !id.pid)
            return false;
        return root._queueVerify(root._verifyKey(id), null, id);
    }

    function _queueVerify(key, handle, id) {
        const cached = root._verifyCache[key];
        if (cached && cached.expires > Date.now())
            return cached.verified;
        delete root._verifyCache[key];
        if (root._verifyInFlight[key] || root._verifyQueue.length >= 128)
            return false;
        root._verifyInFlight[key] = true;
        root._verifyQueue.push({
            key: key,
            handle: (handle === null || handle === undefined) ? null : handle,
            identity: Object.assign({}, id),
            generation: root._verifyGeneration
        });
        root._startNextVerification();
        return false;
    }

    function _startNextVerification() {
        if (root._verifyActive)
            return;
        while (root._verifyQueue.length) {
            const entry = root._verifyQueue.shift();
            // handle === null → wire-sidecar identity (v35): no handle
            // map to drift against, the tuple itself is the subject.
            const current = (entry.handle === null)
                ? entry.identity
                : root._handleToIdentity[entry.handle];
            if (!current || root._verifyKey(current) !== entry.key) {
                delete root._verifyInFlight[entry.key];
                continue;
            }
            const id = entry.identity;
            root._verifyActive = entry;
            _verifyProc.command = ["busctl", "--system", "--no-pager", "--timeout=2s", "call", "org.qdistro.AdminBroker1", "/org/qdistro/AdminBroker1", "org.qdistro.AdminBroker1", "VerifyClientIdentity", "utusssss", String(id.pid >>> 0), String(id.starttime), String(id.uid >>> 0), String(id.exe || ""), String(id.label || ""), String(id.sandboxEngine || ""), String(id.appId || ""), String(id.instanceId || "")];
            _verifyProc.running = true;
            return;
        }
    }

    function _finishVerification(exitCode, output) {
        const entry = root._verifyActive;
        root._verifyActive = null;
        if (entry && entry.generation === root._verifyGeneration) {
            delete root._verifyInFlight[entry.key];
            const current = (entry.handle === null)
                ? entry.identity
                : root._handleToIdentity[entry.handle];
            if (current && root._verifyKey(current) === entry.key) {
                const verified = exitCode === 0 && String(output || "").trim() === "b true";
                root._verifyCache[entry.key] = {
                    verified: verified,
                    expires: Date.now() + (verified ? 30000 : 1000)
                };
            }
        }
        Qt.callLater(root._startNextVerification);
    }

    Process {
        id: _verifyProc
        running: false
        stdout: StdioCollector {
            id: _verifyStdout
        }
        stderr: StdioCollector {
            id: _verifyStderr
        }
        onExited: (exitCode, exitStatus) => root._finishVerification(exitCode, _verifyStdout.text)
    }

    // Derive a stable silo string from a (sandboxEngine, appId,
    // instanceId) tuple.
    // Mirrors the per-engine resolution rules in _onSecurityContext so a
    // wire-sourced tuple (v23 sidecar) and a toplevel-handle-sourced
    // tuple (v13 toplevel_security_context) yield the same silo string
    // for the same client. instance_id is a launch-correlation token for
    // qdistro tier2/tier3/tier5 and must not enter clipboard silo identity.
    function _siloFromSecctx(sandboxEngine, appId, instanceId) {
        return ClipboardSilo.fromSecctx(sandboxEngine, appId, instanceId);
    }

    // v23 sidecar ↔ handle binding (paravirt ΔB10): when the sidecar
    // supplied the source silo, the handle's relayed (pid, starttime) is
    // trustworthy ONLY if the handle's attested secctx tuple is identical
    // to the sidecar's — i.e. the tagged source IS the focused toplevel's
    // client (the waypipe bridge case: one wl_client owns both the window
    // and the offer). A tagged source that does not own the focused
    // toplevel — a background helper, a foreign launch — can never match:
    // its sidecar instance differs from the focused toplevel's, so it
    // keeps relaying 0/0 (enforce-mode deny at the broker). Both sides
    // of the comparison are compositor-emitted wp_security_context_v1
    // tags the client cannot rewrite, so a hostile client cannot pick
    // the match. instanceId must be non-empty: two distinct clients of
    // one app would otherwise collide on (engine, appId) alone.
    function _sourceBoundToHandle(pending, handleIdentity) {
        if (pending === null || !handleIdentity) return false;
        const inst = pending.instanceId || "";
        return inst.length > 0
            && inst === (handleIdentity.instanceId || "")
            && (pending.sandboxEngine || "") === (handleIdentity.sandboxEngine || "")
            && (pending.appId || "") === (handleIdentity.appId || "");
    }

    // v23 sidecar handler. Stash the tuple as "pending"; the very next
    // _onSelectionSet consumes it. Overwrites any previous pending entry
    // — by the qdwin contract there is at most one outstanding sidecar
    // per resource, so a back-to-back pair {sidecar, sidecar} would only
    // arise from a bug, and "last write wins" matches what selection_set
    // itself would do.
    function _onSelectionSetSourceIdentity(sandboxEngine, appId, instanceId) {
        root._pendingSrcIdentity = {
            "sandboxEngine": sandboxEngine || "",
            "appId": appId || "",
            "instanceId": instanceId || ""
        };
    }

    // Pure relay-selection rule (Node-testable): which (pid, starttime)
    // tuple may be relayed to the broker as the selection source's
    // identity. v35 wire-attested peer identity ALWAYS wins — it names
    // the exact wl_client that issued set_selection, so it is sound
    // even when no focused toplevel shares the tag (cross-silo offer
    // from a toplevel-less source). Pre-v35 fallback keeps the old
    // contract: handle identity only on the v11 path or a bound
    // sidecar; anything else returns {} so the broker sees 0/0 and
    // enforce denies rather than resolving an unrelated handle.
    function _sourceRelayIdentity(pendingPeer, pending, bound, handleIdentity) {
        if (pendingPeer && pendingPeer.pid) return pendingPeer;
        if (pending === null || bound) return handleIdentity || {};
        return {};
    }

    // v35 sidecar handler. The peer tuple belongs to the very wl_client
    // that issued set_selection — NOT to whichever focused toplevel
    // happens to carry the same (engine, appId, instanceId) tag. Merge
    // the tag fields from _pendingSrcIdentity (which always precedes
    // this event per the wire contract) so the stashed identity is a
    // complete verifyKey-shaped tuple; consumed on the next
    // _onSelectionSet alongside _pendingSrcIdentity.
    function _onSelectionSetSourcePeerIdentity(peerPid, peerStarttime, peerUid, peerExe, peerSelinuxLabel) {
        const pending = root._pendingSrcIdentity || {};
        root._pendingSrcPeer = {
            "pid": peerPid >>> 0,
            "starttime": peerStarttime,
            "uid": peerUid >>> 0,
            "exe": peerExe || "",
            "label": peerSelinuxLabel || "",
            "sandboxEngine": pending.sandboxEngine || "",
            "appId": pending.appId || "",
            "instanceId": pending.instanceId || ""
        };
    }

    function _onSecurityContext(handle, sandboxEngine, appId, instanceId) {
        const stableSilo = root._siloFromSecctx(sandboxEngine, appId, instanceId);
        if (stableSilo.length > 0) {
            root._handleToSilo[handle] = stableSilo;
        } else {
            // A security_context event arrived but carries no stable
            // identity (e.g. missing app_id). This is a TAGGED client we
            // cannot pin to a silo, so it must fail safe — overwrite the
            // uid-derived placeholder from _onToplevelAdded with "unknown"
            // rather than let it silently group same-silo by uid. (The uid
            // placeholder is only meant for clients that NEVER get a
            // security_context event.)
            root._handleToSilo[handle] = "unknown";
        }
        if (appId && appId.length > 0) {
            root._handleToAppId[handle] = appId;
        } else {
            // No app_id on a tagged client — drop any stale app_id so the
            // tier MIME-strip / broker action key can't be derived from a
            // leftover placeholder.
            delete root._handleToAppId[handle];
        }
        root._handleToSandboxEngine[handle] = sandboxEngine || "";
        // Stash the secctx tuple on the identity entry so the broker
        // VerifyClientIdentity call can include "claimed" values alongside
        // the (pid, starttime, exe, label) the compositor observed.
        const existing = root._handleToIdentity[handle] || {};
        root._handleToIdentity[handle] = Object.assign({}, existing, {
            "sandboxEngine": sandboxEngine || "",
            "appId": appId || "",
            "instanceId": instanceId || ""
        });
    }

    // Tier-4 strict MIME allow-list. The base type (everything before the
    // first ";") must equal text/plain or text/uri-list; charset suffixes
    // are preserved. Mirrors qdistro/tier4-vm/tier4_chrome.py::strip_mimes
    // — Python is the canonical implementation; this is the QML port.
    // (P05a security MS-2 / integration MEDIUM-1.)
    // paravirt ΔB6: the same allow-list is the tier3s policy — a gVisor
    // sandbox feeding a waypipe bridge gets the identical strip until a
    // tier3s-specific MIME policy is designed (03 step 5).
    readonly property var _tier4AllowedMimeBases: ["text/plain", "text/uri-list"]

    // True for a secctx app_id whose OFFERED mimes must pass the strict
    // allow-list: tier-4 VM windows and tier3s (gVisor/waypipe) windows.
    // "qdistro.tier3s." must be tested literally — startsWith("qdistro.tier4.")
    // obviously cannot match it, and "qdistro.tier3." cannot match it either
    // (the trailing '.' disambiguates).
    function _isStrictMimeSource(srcAppId) {
        return srcAppId.startsWith("qdistro.tier4.") || srcAppId.startsWith("qdistro.tier3s.");
    }

    function _strictMimeTier(srcAppId) {
        return srcAppId.startsWith("qdistro.tier3s.") ? "tier3s" : "tier4";
    }

    function _stripTier4Mimes(mimes) {
        const seen = {};
        const out = [];
        for (let i = 0; i < mimes.length; i++) {
            const s = mimes[i];
            if (typeof s !== "string" || s.length === 0)
                continue;
            const base = s.split(";", 1)[0].trim().toLowerCase();
            if (root._tier4AllowedMimeBases.indexOf(base) < 0)
                continue;
            if (seen[s])
                continue;
            seen[s] = true;
            out.push(s);
        }
        return out;
    }

    function _logDecision(entry, verdict, reason) {
        Logger.i("ClipboardGate", "CLIPBOARD_GATE", "seat=" + (entry.seat || "default"), "src_silo=" + entry.srcSilo, "dst_silo=" + entry.dstSilo, "mime_types=" + entry.mimeCsv, "verdict=" + verdict, "reason=" + reason);
    }

    function _logDecisionAndMaybeClear(entry, verdict, reason) {
        root._logDecision(entry, verdict, reason);
        if (verdict === "deny" && root._binding) {
            // Deny-storm coalescer: the FIRST deny for this offer identity
            // always clears (fail-closed); identical repeats inside the window
            // suppress only the redundant clear_selection WIRE write so a
            // re-offer loop can't overflow the 4 KB wl buffer and kill the
            // shell↔compositor connection. The verdict line above is logged
            // unconditionally regardless. See ClipboardDenyCoalesce.js.
            const _nowMs = Date.now();
            if (ClipboardDenyCoalesce.shouldSendClear(root._lastDenyClearByKey, entry, _nowMs, root._denyClearCoalesceMs)) {
                root._binding.clearSelection(entry.seat || "default", entry.isPrimary);
            } else {
                Logger.d("ClipboardGate", "CLIPBOARD_CLEAR_COALESCED", "seat=" + (entry.seat || "default"), "src_silo=" + entry.srcSilo, "dst_silo=" + entry.dstSilo, "is_primary=" + entry.isPrimary, "reason=" + reason);
            }
        }
    }

    // -- the gate itself -------------------------------------------------
    function _onSelectionSet(seat, sourceHandle, mimeTypesConcat, isPrimary) {
        // v23 wire-sourced identity wins over the focus-handle map. The
        // compositor only emits the sidecar when the source wl_client
        // carries a wp_security_context_v1 tag, so a non-null
        // _pendingSrcIdentity means "we know the source silo from the
        // wire — don't trust the focus-handle map" (which collapses to the
        // focused admin shell's silo when the tagged client doesn't own a
        // focused toplevel). Consume + clear.
        const pending = root._pendingSrcIdentity;
        root._pendingSrcIdentity = null;
        // v35 wire-attested peer identity of the actual selection-source
        // wl_client (null on pre-v35 compositors or untagged sources).
        const pendingPeer = root._pendingSrcPeer;
        root._pendingSrcPeer = null;
        let srcSilo;
        if (pending !== null) {
            // Tagged source: trust the wire identity, NOT the focus-handle
            // map (sourceHandle here can name the focused destination/admin
            // toplevel, not the tagged source). If the wire tuple yields no
            // stable silo — e.g. app_id missing — fail safe to "unknown"
            // rather than borrowing the focus handle's (possibly uid-
            // placeholder) silo, which could falsely read as same-silo.
            const wireSilo = root._siloFromSecctx(pending.sandboxEngine, pending.appId, pending.instanceId);
            srcSilo = wireSilo.length > 0 ? wireSilo : "unknown";
        } else {
            srcSilo = root._handleToSilo[sourceHandle] || "unknown";
        }
        // Destination silo = silo of the currently-focused toplevel on this
        // seat. The binding caches focusedHandle on the seat that last
        // changed; for Phase-1 (single seat) we just read that.
        const focusedHandle = root._binding ? root._binding.focusedHandle : 4294967295;
        const dstSilo = (focusedHandle !== 4294967295) ? (root._handleToSilo[focusedHandle] || "unknown") : "unknown";
        let mimeList = (mimeTypesConcat || "").split("\n").filter(s => s.length > 0);

        // Tier-4 / tier3s source → strict MIME allow-list (text/plain +
        // text/uri-list). The strip runs BEFORE policy consult so a tier-4
        // guest or tier3s sandbox advertising text/html or image/png has
        // those types dropped, not evaluated.
        // (P05a security MS-2 / integration MEDIUM-1; paravirt ΔB6.)
        const srcAppId = (pending !== null && pending.appId) ? pending.appId : (root._handleToAppId[sourceHandle] || "");
        if (root._isStrictMimeSource(srcAppId)) {
            const before = mimeList.length;
            mimeList = root._stripTier4Mimes(mimeList);
            if (mimeList.length !== before) {
                Logger.i("ClipboardGate", root._strictMimeTier(srcAppId) + " mime-strip", "src_app=" + srcAppId, "before=" + before, "after=" + mimeList.length);
            }
        }
        const mimeCsv = mimeList.join(",");
        const decisionEntry = {
            "seat": seat || "default",
            "isPrimary": isPrimary,
            "srcSilo": srcSilo,
            "dstSilo": dstSilo,
            "mimeCsv": mimeCsv
        };

        // focus-aware-clear (clipboard.md §"focus-aware-clear"): remember
        // the silo that now owns this selection kind, so a later focus
        // change out of that silo can clear it. Recorded regardless of the
        // allow/deny verdict below — a denied set is already cleared, but
        // recording it keeps the source-silo state truthful and harmless
        // (the entry simply describes whoever last held the offer). A
        // resolved "unknown" src silo is dropped: there is nothing
        // trustworthy to compare against, and tracking it would clear on
        // every subsequent focus change (default-deny is enforced at
        // set/receive time instead).
        const _selKind = isPrimary ? "1" : "0";
        root._selectionGen[_selKind] = (root._selectionGen[_selKind] || 0) + 1;
        const _selGen = root._selectionGen[_selKind];
        if (srcSilo !== "unknown") {
            root._selectionSourceSilo[_selKind] = srcSilo;
        } else {
            delete root._selectionSourceSilo[_selKind];
        }

        // If after stripping there are no allowed MIMEs, deny without
        // consulting policy. The Python strip_mimes contract is "deny on
        // empty stripped list" — keep that semantics here.
        if (root._isStrictMimeSource(srcAppId) && mimeList.length === 0) {
            root._logDecisionAndMaybeClear(decisionEntry, "deny", root._strictMimeTier(srcAppId) + "-no-allowed-mimes");
            return;
        }

        // Option-B identity gate (todo/decisions/secctx-identity-contract.md):
        // the same-silo string match short-circuits to allow only when the
        // broker has independently re-verified the source AND destination
        // process identity against /proc. Without verification (broker
        // absent, race before first verify, mismatch), fall through to the
        // cross-silo policy path — which is default-deny. _ensureVerified
        // returns synchronously cached results and fires off an async
        // broker round-trip on first sight.
        // If the v23 wire sidecar supplied the source silo, sourceHandle is
        // only trusted for source identity when it is bound to the sidecar
        // (the focused toplevel's attested tag IS the source's tag — the
        // single-client bridge case). Unbound: it can name the focused
        // destination/admin toplevel — fail closed.
        // v35 narrows this further: when the peer-identity sidecar arrived,
        // it names the exact wl_client that issued set_selection — verify
        // THAT tuple, not whichever focused handle shares the tag.
        const _bound = root._sourceBoundToHandle(
            pending, root._handleToIdentity[sourceHandle]);
        const _srcPeer = (pendingPeer && pendingPeer.pid) ? pendingPeer : null;
        const srcVerified = _srcPeer
            ? root._ensureVerifiedIdentity(_srcPeer)
            : ((pending === null || _bound) ? root._ensureVerified(sourceHandle) : false);
        const dstVerified = (focusedHandle !== 4294967295) ? root._ensureVerified(focusedHandle) : false;
        const identityVerified = srcVerified && dstVerified;
        if (!ClipboardBroker.hasKnownIdentity(srcSilo, dstSilo)) {
            root._logDecisionAndMaybeClear(decisionEntry, "deny", "unknown-identity");
            return;
        }

        const dstAppId = (focusedHandle !== 4294967295) ? (root._handleToAppId[focusedHandle] || "") : "";
        const sourceSandboxEngine = (pending !== null && pending.sandboxEngine) ? pending.sandboxEngine : (root._handleToSandboxEngine[sourceHandle] || "");
        if (!root._binding || root._binding.checkClipboardTransfer === undefined) {
            root._logDecisionAndMaybeClear(decisionEntry, "deny", "broker-unavailable");
            return;
        }
        // Relay the source app's kernel-authenticated (pid, starttime) so
        // the broker can attest the source silo via its launch-record
        // store (P1-1). _sourceRelayIdentity encodes the precedence:
        // v35 wire-attested source peer > bound/v11 handle identity >
        // 0/0 fail-closed.
        const _srcId = root._sourceRelayIdentity(
            _srcPeer, pending, _bound, root._handleToIdentity[sourceHandle]);
        if (root._asyncChecks) {
            const requestId = root._binding.startCheckClipboardTransfer(srcSilo, dstSilo, mimeList, srcAppId, dstAppId, sourceSandboxEngine, identityVerified, (_srcId.pid >>> 0) || 0, _srcId.starttime || 0);
            root._pendingChecks[requestId] = {
                "kind": "transfer",
                "entry": decisionEntry,
                "selKind": _selKind,
                "gen": _selGen
            };
            return;
        }
        const brokerResult = root._binding.checkClipboardTransfer(srcSilo, dstSilo, mimeList, srcAppId, dstAppId, sourceSandboxEngine, identityVerified, (_srcId.pid >>> 0) || 0, _srcId.starttime || 0);
        const decision = ClipboardBroker.parseCheckClipboardTransferResult(brokerResult.exitCode, brokerResult.stdout || "");
        root._logDecisionAndMaybeClear(decisionEntry, decision.verdict, decision.reason);
    }

    // Completion of a startCheckClipboard* request. Unknown ids (a lost
    // connection's requests, or a duplicate report) are ignored. The broker
    // reply format and its fail-closed parsing are the synchronous path's.
    function _onClipboardCheckFinished(requestId, exitCode, stdoutText, timedOut) {
        const check = root._pendingChecks[requestId];
        if (!check)
            return;
        delete root._pendingChecks[requestId];
        const decision = ClipboardBroker.parseCheckClipboardTransferResult(exitCode, stdoutText || "");
        if (check.kind === "receive") {
            root._answerReceive(check.requestHandle, check.seat, check.srcSilo, check.dstSilo, check.mime, decision.verdict, decision.reason);
            return;
        }
        if (root._selectionGen[check.selKind] !== check.gen) {
            // Superseded: record the verdict, but a deny here must not clear
            // the selection that replaced this one.
            root._logDecision(check.entry, decision.verdict, decision.reason);
            Logger.d("ClipboardGate", "CLIPBOARD_GATE_SUPERSEDED", "seat=" + (check.entry.seat || "default"), "src_silo=" + check.entry.srcSilo, "is_primary=" + check.entry.isPrimary);
            return;
        }
        root._logDecisionAndMaybeClear(check.entry, decision.verdict, decision.reason);
    }

    // -- focus-aware-clear (qdwin_shell_v1 seat_focus_changed) ----------
    // Qubes-style mitigation: when keyboard focus crosses out of the silo
    // that owns the active selection, clear that selection for the newly
    // focused (destination) silo so a cross-silo paste can't even reach a
    // stale offer. Same-silo focus changes are a no-op — same-silo paste
    // must keep working. Fail-closed posture: an unknown source silo is
    // never tracked (so nothing to clear here), and an unknown destination
    // silo always differs from any known source silo, so it clears.
    function _onSeatFocusChanged(seat, handle) {
        // Unlike the set-time gate (which reads the single cached focused
        // handle — Phase-1 single-seat), this path is per-seat-safe: it
        // forwards the event's own `seat` straight through to clearSelection,
        // so a multi-seat compositor clears only the seat whose focus moved.
        // Pure decision lives in ClipboardFocusClear.js (unit-tested from
        // Node). It returns the ordered list of selection kinds to clear
        // because focus crossed OUT of their source silo; same-silo and
        // untracked kinds are omitted (no clear). We perform the identical
        // side effects per action: journal the deny, clear the selection,
        // and forget the now-cleared source so we don't re-clear on the
        // next focus change.
        const actions = ClipboardFocusClear.planFocusClear(seat, handle, root._handleToSilo, root._selectionSourceSilo);
        for (let i = 0; i < actions.length; i++) {
            const a = actions[i];
            root._logFocusClear(seat, a.srcSilo, a.dstSilo, a.isPrimary);
            if (root._binding) {
                root._binding.clearSelection(seat || "default", a.isPrimary);
            }
            delete root._selectionSourceSilo[a.selKind];
        }
    }

    // Structured journal verdict for a focus-aware clear. Mirrors the
    // CLIPBOARD_GATE / CLIPBOARD_RECEIVE_GATE line shape — field order is a
    // stable contract. The set-time CLIPBOARD_GATE / receive-time
    // CLIPBOARD_RECEIVE_GATE lines are asserted by the qdistro VM harness; the
    // pure decision is also covered headlessly by the Node unit test
    // tests/test_clipboard_focus_clear.js. This line IS now asserted in the VM
    // too: the inject-focus CLI exists (Tier3FocusIPC `injectFocus` →
    // Qdwin.injectFocus → qdwin_shell_v1), so the qdistro probe
    // s49-clipboard-focus-gate-journal.sh (wrapped by tiered-isolation.bats
    // `phase7-clipboard-focus-gate-journal`) drives a real cross-silo focus
    // transition and greps for this exact line —
    //   CLIPBOARD_FOCUS_GATE ... src_silo=... dst_silo=... verdict=deny reason=focus-cross-silo
    // — so the field names/order below are a load-bearing contract with that
    // probe; changing them requires updating s49 in lockstep.
    function _logFocusClear(seat, srcSilo, dstSilo, isPrimary) {
        Logger.i("ClipboardGate", "CLIPBOARD_FOCUS_GATE", "seat=" + (seat || "default"), "src_silo=" + srcSilo, "dst_silo=" + dstSilo, "is_primary=" + isPrimary, "verdict=deny", "reason=focus-cross-silo");
    }

    // -- receive-time gate (qdwin_shell_v1 v15+) ------------------------
    // Mirrors _onSelectionSet but for a SINGLE requested mime: qdwin blocks
    // the destination fd for ~2s awaiting sendDataOfferReceiveDecision(
    // requestHandle, allow). The broker step is asynchronous (answered from
    // _onClipboardCheckFinished within 1.5 s); every local deny answers at
    // once. We MUST send exactly once on every path — every error/fallback
    // denies (fail-closed) rather than letting the compositor time out.
    function _logReceiveDecision(seat, srcSilo, dstSilo, mimeType, verdict, reason) {
        Logger.i("ClipboardGate", "CLIPBOARD_RECEIVE_GATE", "seat=" + (seat || "default"), "src_silo=" + srcSilo, "dst_silo=" + dstSilo, "mime=" + mimeType, "verdict=" + verdict, "reason=" + reason);
    }

    // Single-exit decision sink: log + answer the compositor exactly once.
    // The decision MUST always be sent — any path reaching here was
    // triggered by dataOfferReceivePending, which only a v15+ binding
    // emits, and that same binding always exposes
    // sendDataOfferReceiveDecision (added in lockstep). We therefore call
    // it unconditionally on the live binding rather than guarding with
    // `!== undefined`, so a gated receive can never be silently dropped
    // (which would let qdwin time out instead of getting our deny). A
    // missing method here can only mean a build mismatch — log loudly.
    function _answerReceive(requestHandle, seat, srcSilo, dstSilo, mimeType, verdict, reason) {
        root._logReceiveDecision(seat, srcSilo, dstSilo, mimeType, verdict, reason);
        if (root._binding && root._binding.sendDataOfferReceiveDecision !== undefined) {
            root._binding.sendDataOfferReceiveDecision(requestHandle, verdict === "allow");
        } else {
            // Unreachable in a correctly-built shell: the event can only
            // originate from a binding that also has the sender. If it
            // happens, the compositor falls back to its own ~2s deny.
            Logger.e("ClipboardGate", "CLIPBOARD_RECEIVE_GATE cannot send decision: binding missing sendDataOfferReceiveDecision", "request_handle=" + requestHandle, "intended_verdict=" + verdict);
        }
    }

    function _onDataOfferReceivePending(requestHandle, seat, sourceHandle, targetHandle, mimeType) {
        const mime = mimeType || "";
        // UINT32_MAX (no toplevel maps) → "unknown" → fail-closed below.
        const srcSilo = root._handleToSilo[sourceHandle] || "unknown";
        // Receive carries an explicit target_handle (the receiving client),
        // not the keyboard-focused toplevel as at set time.
        const dstSilo = root._handleToSilo[targetHandle] || "unknown";
        const srcAppId = root._handleToAppId[sourceHandle] || "";

        // Tier-4 / tier3s source → strict MIME allow-list (text/plain +
        // text/uri-list). A single requested mime that strips to empty →
        // deny (paravirt ΔB6: same rule for both tiers).
        if (root._isStrictMimeSource(srcAppId)) {
            const kept = root._stripTier4Mimes([mime]);
            if (kept.length === 0) {
                root._answerReceive(requestHandle, seat, srcSilo, dstSilo, mime, "deny", root._strictMimeTier(srcAppId) + "-no-allowed-mimes");
                return;
            }
        }

        // Unknown source or destination identity → fail-closed: no
        // trustworthy action key for rules/cache lookup.
        if (!ClipboardBroker.hasKnownIdentity(srcSilo, dstSilo)) {
            root._answerReceive(requestHandle, seat, srcSilo, dstSilo, mime, "deny", "unknown-identity");
            return;
        }

        // Option-B identity verification — both ends. UINT32_MAX handles
        // are never verified (_ensureVerified returns false on no identity).
        const srcVerified = (sourceHandle !== 4294967295) ? root._ensureVerified(sourceHandle) : false;
        const dstVerified = (targetHandle !== 4294967295) ? root._ensureVerified(targetHandle) : false;
        const identityVerified = srcVerified && dstVerified;

        if (!root._binding || root._binding.checkClipboardReceive === undefined) {
            root._answerReceive(requestHandle, seat, srcSilo, dstSilo, mime, "deny", "broker-unavailable");
            return;
        }

        const dstAppId = root._handleToAppId[targetHandle] || "";
        const sourceSandboxEngine = root._handleToSandboxEngine[sourceHandle] || "";
        // Relay the source app's authenticated (pid, starttime) for
        // launch-record attestation of the source silo (P1-1).
        const _srcId = root._handleToIdentity[sourceHandle] || {};
        if (root._asyncChecks) {
            // Answered from _onClipboardCheckFinished; the binding reports
            // every request within 1.5 s, inside qdwin's 2 s receive timer.
            const requestId = root._binding.startCheckClipboardReceive(srcSilo, dstSilo, mime, srcAppId, dstAppId, sourceSandboxEngine, identityVerified, (_srcId.pid >>> 0) || 0, _srcId.starttime || 0);
            root._pendingChecks[requestId] = {
                "kind": "receive",
                "requestHandle": requestHandle,
                "seat": seat,
                "srcSilo": srcSilo,
                "dstSilo": dstSilo,
                "mime": mime
            };
            return;
        }
        const brokerResult = root._binding.checkClipboardReceive(srcSilo, dstSilo, mime, srcAppId, dstAppId, sourceSandboxEngine, identityVerified, (_srcId.pid >>> 0) || 0, _srcId.starttime || 0);
        // The broker returns a bare "allow"/"deny" string (busctl prints
        // `s "allow"`). Reuse the set-time parser — same wire format,
        // same fail-closed semantics on nonzero exit/timeout/malformed.
        const decision = ClipboardBroker.parseCheckClipboardTransferResult(brokerResult.exitCode, brokerResult.stdout || "");
        root._answerReceive(requestHandle, seat, srcSilo, dstSilo, mime, decision.verdict, decision.reason);
    }
}
