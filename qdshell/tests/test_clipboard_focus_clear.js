const assert = require("assert");
const FocusClear = require("../Services/Qdshell/ClipboardFocusClear.js");

// Focus-aware-clear primitive (clipboard.md §"focus-aware-clear", track-04
// Phase-2). The pure decision is ClipboardFocusClear.planFocusClear(); the
// side effects (clearSelection + CLIPBOARD_FOCUS_GATE journal + forgetting
// the cleared source) live in ClipboardGate.qml's _onSeatFocusChanged loop.
// To exercise the WHOLE handler contract — not just the decision — this test
// reproduces that loop verbatim around the real decision module, with a fake
// binding recording clearSelection() calls and a journal sink, exactly as the
// QML does. The decision logic under test is the product module; only the
// thin side-effect wiring (Logger.i / binding.clearSelection / delete) is
// mirrored here.
//
// ensures (overall): keyboard focus crossing OUT of the selection-source silo
// clears that selection (and only that selection) fail-closed, while same-silo
// focus preserves the paste — the user-visible Qubes-style cross-silo barrier.

const NO_HANDLE = FocusClear.NO_HANDLE; // 4294967295

// Build a fresh gate-state harness mirroring the QML singleton's relevant
// fields and the _onSeatFocusChanged side-effect loop.
function makeGate(handleToSilo) {
    const gate = {
        handleToSilo: handleToSilo || {},
        // selectionSourceSilo[selKind] — "0" regular, "1" primary. Present
        // only for a known (trustworthy) source, set by _onSelectionSet.
        selectionSourceSilo: {},
        // recorded side effects:
        clears: [],   // [{ seat, isPrimary }] in call order
        journal: [],  // [{ seat, srcSilo, dstSilo, isPrimary }] in call order
        bindingPresent: true
    };

    // Mirror _onSelectionSet's source-silo bookkeeping: track a known source
    // per kind; an "unknown" source is dropped (nothing trustworthy to clear).
    gate.onSelectionSet = function (isPrimary, srcSilo) {
        const selKind = isPrimary ? "1" : "0";
        if (srcSilo !== "unknown")
            gate.selectionSourceSilo[selKind] = srcSilo;
        else
            delete gate.selectionSourceSilo[selKind];
    };

    // Mirror _onSeatFocusChanged: drive the real decision, then perform the
    // exact side effects in order.
    gate.onSeatFocusChanged = function (seat, handle) {
        const actions = FocusClear.planFocusClear(
            seat, handle, gate.handleToSilo, gate.selectionSourceSilo);
        for (let i = 0; i < actions.length; i++) {
            const a = actions[i];
            // CLIPBOARD_FOCUS_GATE journal verdict (field-order is a stable
            // contract the VM harness asserts on).
            gate.journal.push({
                seat: seat || "default",
                srcSilo: a.srcSilo,
                dstSilo: a.dstSilo,
                isPrimary: a.isPrimary,
                verdict: "deny",
                reason: "focus-cross-silo"
            });
            if (gate.bindingPresent)
                gate.clears.push({ seat: seat || "default", isPrimary: a.isPrimary });
            delete gate.selectionSourceSilo[a.selKind];
        }
    };
    return gate;
}

// Handle→silo fixture: distinct handles in distinct silos, plus a handle
// deliberately absent from the map (unknown destination silo).
const H = {
    workTerm: 10,   // silo "tier2/work"
    workFox: 11,    // silo "tier2/work" (same silo, different window)
    persFox: 20,    // silo "tier3/personal"
    vmApp: 30,      // silo "vm-firefox"
    orphan: 99      // intentionally NOT in the map → unknown silo
};
const SILOS = {
    10: "tier2/work",
    11: "tier2/work",
    20: "tier3/personal",
    30: "vm-firefox"
    // 99 absent on purpose
};

// ---------------------------------------------------------------------------
// 1. Focus crosses to a DIFFERENT silo than the selection source → clear that
//    seat/kind, log a verdict, forget the entry; a second focus event to the
//    same silo does NOT clear again.
// ensures: a cross-silo focus transition clears the stranded selection exactly
//          once and never re-fires for an already-cleared source.
// ---------------------------------------------------------------------------
{
    const g = makeGate(SILOS);
    g.onSelectionSet(false, "tier2/work"); // regular copy in work silo

    g.onSeatFocusChanged("seat0", H.persFox); // focus → personal (different)
    assert.deepStrictEqual(g.clears, [{ seat: "seat0", isPrimary: 0 }]);
    assert.deepStrictEqual(g.journal, [{
        seat: "seat0", srcSilo: "tier2/work", dstSilo: "tier3/personal",
        isPrimary: 0, verdict: "deny", reason: "focus-cross-silo"
    }]);
    // Source forgotten → entry removed.
    assert.strictEqual(g.selectionSourceSilo["0"], undefined);

    // A second focus event (even back to a different silo) must NOT re-clear:
    // the source was forgotten.
    g.onSeatFocusChanged("seat0", H.vmApp);
    assert.deepStrictEqual(g.clears, [{ seat: "seat0", isPrimary: 0 }]); // unchanged
    assert.strictEqual(g.journal.length, 1);
}

// ---------------------------------------------------------------------------
// 2. Focus STAYS in the same silo as the source → clearSelection NOT called.
// ensures: same-silo paste is preserved (work copy → work terminal still pastes).
// ---------------------------------------------------------------------------
{
    const g = makeGate(SILOS);
    g.onSelectionSet(false, "tier2/work");

    // Focus to a DIFFERENT window in the SAME silo.
    g.onSeatFocusChanged("seat0", H.workFox);
    assert.deepStrictEqual(g.clears, []);
    assert.deepStrictEqual(g.journal, []);
    // Source still tracked (not forgotten on a same-silo no-op).
    assert.strictEqual(g.selectionSourceSilo["0"], "tier2/work");

    // Focusing back the original handle in-silo is likewise a no-op, and the
    // selection survives for a later real cross-silo clear.
    g.onSeatFocusChanged("seat0", H.workTerm);
    assert.deepStrictEqual(g.clears, []);
    g.onSeatFocusChanged("seat0", H.persFox);
    assert.deepStrictEqual(g.clears, [{ seat: "seat0", isPrimary: 0 }]);
}

// ---------------------------------------------------------------------------
// 3. Regular vs primary handled INDEPENDENTLY: clearing primary doesn't touch
//    regular, and vice versa.
// ensures: the two selection kinds are isolated — a cross-silo clear of one
//          never strands or preserves the other by accident.
// ---------------------------------------------------------------------------
{
    // 3a. Regular in work, primary in personal. Focus into vm-firefox crosses
    // OUT of BOTH → both clear, in deterministic order (regular then primary).
    const g = makeGate(SILOS);
    g.onSelectionSet(false, "tier2/work");    // regular
    g.onSelectionSet(true, "tier3/personal"); // primary

    g.onSeatFocusChanged("seat0", H.vmApp);   // different from both
    assert.deepStrictEqual(g.clears, [
        { seat: "seat0", isPrimary: 0 },
        { seat: "seat0", isPrimary: 1 }
    ]);
    assert.deepStrictEqual(g.journal.map(j => j.isPrimary), [0, 1]);
    assert.strictEqual(g.selectionSourceSilo["0"], undefined);
    assert.strictEqual(g.selectionSourceSilo["1"], undefined);
}
{
    // 3b. Both kinds in DIFFERENT silos; focus lands IN the primary's silo.
    // Primary stays (same-silo), regular clears (cross-silo). Proves one kind
    // can clear while the other is preserved in the same focus event.
    const g = makeGate(SILOS);
    g.onSelectionSet(false, "tier2/work");    // regular  → work
    g.onSelectionSet(true, "tier3/personal"); // primary  → personal

    g.onSeatFocusChanged("seat0", H.persFox); // dst = personal
    // Only the regular selection crossed out of its silo.
    assert.deepStrictEqual(g.clears, [{ seat: "seat0", isPrimary: 0 }]);
    assert.strictEqual(g.selectionSourceSilo["0"], undefined); // regular gone
    assert.strictEqual(g.selectionSourceSilo["1"], "tier3/personal"); // primary kept
}

// ---------------------------------------------------------------------------
// 4. Unknown/untracked source silo → no clear on any focus change.
// ensures: an "unknown" source is never tracked, so focus transitions cannot
//          fire a clear with nothing trustworthy to compare against.
// ---------------------------------------------------------------------------
{
    const g = makeGate(SILOS);
    // _onSelectionSet with an "unknown" source must NOT track anything.
    g.onSelectionSet(false, "unknown");
    g.onSelectionSet(true, "unknown");
    assert.strictEqual(g.selectionSourceSilo["0"], undefined);
    assert.strictEqual(g.selectionSourceSilo["1"], undefined);

    // Any subsequent focus change — even into a known different silo — clears
    // nothing.
    g.onSeatFocusChanged("seat0", H.persFox);
    g.onSeatFocusChanged("seat0", H.vmApp);
    g.onSeatFocusChanged("seat0", NO_HANDLE);
    assert.deepStrictEqual(g.clears, []);
    assert.deepStrictEqual(g.journal, []);
}

// ---------------------------------------------------------------------------
// 5. Newly-focused handle has an UNKNOWN silo (not in handle→silo map) while a
//    known source is tracked → treated as different → CLEARS (fail-closed).
// ensures: an unresolvable destination is never assumed same-silo; the
//          selection is cleared rather than left exposed.
// ---------------------------------------------------------------------------
{
    // 5a. Orphan handle (absent from the map) resolves to "unknown" → clears.
    const g = makeGate(SILOS);
    g.onSelectionSet(false, "tier2/work");
    g.onSeatFocusChanged("seat0", H.orphan);
    assert.deepStrictEqual(g.clears, [{ seat: "seat0", isPrimary: 0 }]);
    assert.strictEqual(g.journal[0].dstSilo, "unknown");
    assert.strictEqual(g.journal[0].srcSilo, "tier2/work");
}
{
    // 5b. NO_HANDLE (focus lost, no toplevel) likewise resolves to "unknown"
    // and clears a tracked source — fail-closed when focus goes nowhere.
    const g = makeGate(SILOS);
    g.onSelectionSet(true, "vm-firefox"); // primary tracked
    g.onSeatFocusChanged("seat0", NO_HANDLE);
    assert.deepStrictEqual(g.clears, [{ seat: "seat0", isPrimary: 1 }]);
    assert.strictEqual(g.journal[0].dstSilo, "unknown");
}

// ---------------------------------------------------------------------------
// 6. _onSelectionSet UPDATES the tracked source silo: a re-copy in a NEW silo
//    retargets which focus changes trigger a clear.
// ensures: re-copying in a different silo moves the cross-silo boundary; focus
//          into the OLD source silo no longer preserves, focus into the NEW
//          source silo does.
// ---------------------------------------------------------------------------
{
    const g = makeGate(SILOS);
    g.onSelectionSet(false, "tier2/work");          // first copy: work
    assert.strictEqual(g.selectionSourceSilo["0"], "tier2/work");

    g.onSelectionSet(false, "tier3/personal");      // re-copy: personal
    assert.strictEqual(g.selectionSourceSilo["0"], "tier3/personal");

    // Focus into the OLD source silo (work) is now CROSS-silo → clears.
    g.onSeatFocusChanged("seat0", H.workTerm);
    assert.deepStrictEqual(g.clears, [{ seat: "seat0", isPrimary: 0 }]);
    assert.strictEqual(g.journal[0].srcSilo, "tier3/personal");
    assert.strictEqual(g.journal[0].dstSilo, "tier2/work");
}
{
    // Re-target preserves same-silo paste for the NEW source: focus into the
    // new silo is a no-op.
    const g = makeGate(SILOS);
    g.onSelectionSet(false, "tier2/work");
    g.onSelectionSet(false, "tier3/personal"); // retarget to personal
    g.onSeatFocusChanged("seat0", H.persFox);  // into new source silo
    assert.deepStrictEqual(g.clears, []);
    assert.strictEqual(g.selectionSourceSilo["0"], "tier3/personal");
}

// ---------------------------------------------------------------------------
// 7. Rapid focus A -> B -> A: a cross-silo clear forgets the source, so
//    bouncing focus straight back to the ORIGINAL source silo does NOT
//    resurrect or re-clear the (now gone) selection. Guards against a
//    forgotten-source regression where focus toggling re-fires verdicts.
// ensures: after a cross-silo clear the source is gone for good; no later
//          focus transition (incl. back into the old source silo) re-fires.
// ---------------------------------------------------------------------------
{
    const g = makeGate(SILOS);
    g.onSelectionSet(false, "tier2/work");   // copy in work (A)

    g.onSeatFocusChanged("seat0", H.persFox); // A -> B (personal): clears
    assert.deepStrictEqual(g.clears, [{ seat: "seat0", isPrimary: 0 }]);
    assert.strictEqual(g.selectionSourceSilo["0"], undefined);

    // B -> A (back into the original work silo): source already forgotten,
    // so NOTHING happens — no re-clear, no second verdict.
    g.onSeatFocusChanged("seat0", H.workTerm);
    assert.deepStrictEqual(g.clears, [{ seat: "seat0", isPrimary: 0 }]); // unchanged
    assert.strictEqual(g.journal.length, 1);
}

// ---------------------------------------------------------------------------
// 8. "unknown" source set (untracked) FOLLOWED BY a valid set re-establishes
//    tracking, so the later valid selection IS cleared on a cross-silo focus.
//    Guards against the untracked-then-valid sequence leaving the second,
//    trustworthy selection un-protected.
// ensures: an earlier untracked "unknown" set does not poison a subsequent
//          valid set — the valid one is tracked and cleared fail-closed.
// ---------------------------------------------------------------------------
{
    const g = makeGate(SILOS);
    g.onSelectionSet(false, "unknown");      // untracked
    assert.strictEqual(g.selectionSourceSilo["0"], undefined);

    g.onSelectionSet(false, "tier2/work");   // now a valid set → tracked
    assert.strictEqual(g.selectionSourceSilo["0"], "tier2/work");

    g.onSeatFocusChanged("seat0", H.persFox); // cross-silo → clears
    assert.deepStrictEqual(g.clears, [{ seat: "seat0", isPrimary: 0 }]);
    assert.strictEqual(g.journal[0].srcSilo, "tier2/work");
    assert.strictEqual(g.journal[0].dstSilo, "tier3/personal");
}

// ---------------------------------------------------------------------------
// 9. A valid tracked set FOLLOWED BY an "unknown" set CLEARS the tracking
//    (the new owner is untrustworthy) so focus changes become a no-op until a
//    new trustworthy source sets the selection.
// ensures: a valid->unknown transition drops the stale known source rather
//          than leaving it tracked against a now-unknown owner.
// ---------------------------------------------------------------------------
{
    const g = makeGate(SILOS);
    g.onSelectionSet(false, "tier2/work");   // tracked
    g.onSelectionSet(false, "unknown");      // ownership now untrustworthy
    assert.strictEqual(g.selectionSourceSilo["0"], undefined);

    g.onSeatFocusChanged("seat0", H.persFox); // nothing trustworthy to clear
    assert.deepStrictEqual(g.clears, []);
    assert.deepStrictEqual(g.journal, []);
}

// ---------------------------------------------------------------------------
// Direct unit checks of the decision module (no harness), pinning the exact
// returned action shape and the resolveDstSilo fail-closed mapping.
// ensures: the pure planFocusClear contract the QML loop depends on is stable.
// ---------------------------------------------------------------------------
{
    // resolveDstSilo: present handle → its silo; absent/NO_HANDLE → "unknown".
    assert.strictEqual(FocusClear.resolveDstSilo(10, SILOS), "tier2/work");
    assert.strictEqual(FocusClear.resolveDstSilo(99, SILOS), "unknown");
    assert.strictEqual(FocusClear.resolveDstSilo(NO_HANDLE, SILOS), "unknown");

    // Empty source map → no actions regardless of destination.
    assert.deepStrictEqual(FocusClear.planFocusClear("s", 20, SILOS, {}), []);

    // Action shape for a cross-silo regular selection.
    const acts = FocusClear.planFocusClear("s", 20, SILOS, { "0": "tier2/work" });
    assert.deepStrictEqual(acts, [{
        selKind: "0", isPrimary: 0, seat: "s",
        srcSilo: "tier2/work", dstSilo: "tier3/personal"
    }]);

    // Falsy seat normalizes to "default" inside the action.
    const def = FocusClear.planFocusClear("", 20, SILOS, { "1": "vm-firefox" });
    assert.strictEqual(def[0].seat, "default");
    assert.strictEqual(def[0].isPrimary, 1);
}

console.log("clipboard-focus-clear: all assertions passed");
