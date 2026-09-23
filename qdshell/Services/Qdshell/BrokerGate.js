function parseStringVerdict(exitCode, stdout, unavailableReason) {
    if (exitCode !== 0)
        return {
            verdict: "deny",
            reason: unavailableReason || "broker-unavailable",
        };

    const out = String(stdout || "").trim();
    const match = out.match(/^s\s+"([^"]+)"$/);
    if (!match)
        return {
            verdict: "deny",
            reason: "broker-malformed",
        };

    if (match[1] === "allow")
        return {
            verdict: "allow",
            reason: "broker:allow",
        };

    if (match[1] === "deny")
        return {
            verdict: "deny",
            reason: "broker:deny",
        };

    return {
        verdict: "deny",
        reason: "broker:" + match[1],
    };
}

function qdwinDecision(verdict) {
    return verdict === "allow" ? 0 : 1;
}

function nestedProxyAction(appId) {
    return "qdistro.nested.advertise:" + String(appId || "");
}

function nestedProxyDetails(appId, originUid) {
    return {
        app_id: String(appId || ""),
        origin_uid: Number(originUid || 0),
    };
}

function knownSilo(silo) {
    return Boolean(silo && silo !== "unknown");
}

if (typeof module !== "undefined") {
    module.exports = {
        parseStringVerdict: parseStringVerdict,
        qdwinDecision: qdwinDecision,
        nestedProxyAction: nestedProxyAction,
        nestedProxyDetails: nestedProxyDetails,
        knownSilo: knownSilo,
    };
}
