function tierSuffix(appId, prefix) {
    if (!appId || !appId.startsWith(prefix))
        return "";
    return appId.slice(prefix.length);
}

// Derive a STABLE clipboard silo identity from the security-context
// (sandbox_engine, app_id) pair. The third argument, instance_id, is a
// PER-LAUNCH correlation token (qdistro tier2/tier3/tier5 stamp it as a
// random LAUNCH_TOKEN per spawn — see qdistro/tier{2,3,5}*/spawn-*.sh).
// It MUST NOT influence the silo: two windows from the same silo carry
// different instance_ids, so feeding it in would split one silo into
// per-window identities and make broker action/audit keys per-launch.
// instanceId is accepted for call-site symmetry only and is ignored.
// When no stable identity can be derived (engine/app_id both empty) we
// return "" so the caller fails safe (treats the client as unknown /
// not same-silo) rather than colliding unrelated clients onto a shared
// constant.
function fromSecctx(sandboxEngine, appId, instanceId) {
    void instanceId; // launch-correlation token only; never a silo key
    const engine = sandboxEngine || "";
    const app = appId || "";

    let tag = tierSuffix(app, "qdistro.tier2.");
    if (engine === "qdistro.tier2" && tag.length > 0)
        return "tier2/" + tag.split("/", 1)[0];

    // paravirt ΔB6: tier3s (gVisor + waypipe bridge) maps to the BARE silo,
    // like tier-3 — never "tier3s/<silo>" (the canonical-silo-key decision:
    // clipboard, launch records and broker lineage all key on <silo>).
    // This engine check is exact equality so "qdistro.tier3s" cannot land
    // here even via the tier3 branch, but the branch is still kept BEFORE
    // tier3 as a mirror of TaskbarLogic.siloTierKey, where the ordering is
    // load-bearing (a "qdistro.tier3"-prefix engine compare does collide).
    tag = tierSuffix(app, "qdistro.tier3s.");
    if (engine === "qdistro.tier3s" && tag.length > 0)
        return tag;

    tag = tierSuffix(app, "qdistro.tier3.");
    if (engine === "qdistro.tier3" && tag.length > 0)
        return tag;

    tag = tierSuffix(app, "qdistro.tier4.");
    if (engine === "qdistro.tier4" && tag.length > 0)
        return tag;

    tag = tierSuffix(app, "qdistro.tier5.");
    if (engine === "qdistro.tier5" && tag.length > 0)
        return "vm-" + tag;

    if (engine === "qdistro.tier2" && app.length > 0)
        return "tier2/" + app.split("/", 1)[0];

    if (engine === "qdistro-silo" && app.length > 0)
        return app;

    // Generic fallback for any other engine (qdistro.* or third-party
    // such as flatpak): the silo is the stable engine:app_id pair. Never
    // the instance_id. We require BOTH engine and app_id so the key is a
    // real, namespaced identity.
    if (engine.length > 0 && app.length > 0)
        return engine + ":" + app;

    // No usable (engine, app_id) identity — e.g. app_id missing. Fail
    // safe with "" so the caller treats the client as unknown (not
    // same-silo). We deliberately do NOT fall back to an engine-only
    // bucket: that would collapse every unrelated client of one engine
    // (and any tier client with a missing app_id, bypassing its tier
    // MIME stripping) into a single shared silo. And we never fall back
    // to instance_id, the per-launch correlation token.
    return "";
}

if (typeof module !== "undefined") {
    module.exports = {
        fromSecctx: fromSecctx,
    };
}
