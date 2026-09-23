function parseCheckClipboardTransferResult(exitCode, stdout) {
    if (exitCode !== 0)
        return {
            verdict: "deny",
            reason: "broker-unavailable",
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
        reason: "broker-unknown-verdict",
    };
}

function hasKnownIdentity(sourceSilo, destSilo) {
    return Boolean(sourceSilo && destSilo
        && sourceSilo !== "unknown"
        && destSilo !== "unknown");
}

if (typeof module !== "undefined") {
    module.exports = {
        parseCheckClipboardTransferResult: parseCheckClipboardTransferResult,
        hasKnownIdentity: hasKnownIdentity,
    };
}
