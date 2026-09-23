// Drift guard for the cross-repo golden frames.
//
// `tests/fixtures/golden-frames.js` is the single source of truth for
// the qdistro bridge wire protocol, but it physically lives as a
// byte-identical copy in two independent git repos (there is no npm
// workspace / shared package linking them — see the header in
// golden-frames.js):
//
//     qdchrome-extension/tests/fixtures/golden-frames.js
//     qdfirefox-extension/tests/fixtures/golden-frames.js
//
// This test makes the "keep in sync by hand" rule machine-checked: when
// the sibling repo is reachable (checked out side-by-side, or pointed at
// via $QDISTRO_SIBLING_GOLDEN), the two copies must be byte-for-byte
// equal. Editing one copy without mirroring the edit fails here.
//
// In single-repo CI the sibling is not present; we still assert the
// local copy parses to the canonical shape, and log that the cross-repo
// comparison was skipped for lack of the sibling — it is NOT silently
// green-by-omission of a product failure, only of an environmental one.
// Release CI that vendors both repos (or sets $QDISTRO_SIBLING_GOLDEN)
// should set $QDISTRO_REQUIRE_SIBLING=1, which turns a missing sibling
// into a hard failure so the cross-repo guard cannot be silently absent
// at the gate that actually ships the extensions.
//
// ensures: the two extensions cannot drift apart on the bridge protocol
// frames without a red test.
import { describe, it, expect } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { INBOUND, OUTBOUND } from "./fixtures/golden-frames.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const LOCAL = path.join(__dirname, "fixtures", "golden-frames.js");

// This repo is qdfirefox-extension; its sibling is qdchrome-extension.
// Default to the side-by-side layout (…/qdchrome-extension and
// …/qdfirefox-extension share a parent); override with an env var.
const SIBLING =
  process.env.QDISTRO_SIBLING_GOLDEN ||
  path.resolve(
    __dirname, "..", "..",
    "qdchrome-extension", "tests", "fixtures", "golden-frames.js",
  );

describe("golden frames — cross-repo drift guard", () => {
  it("local fixture exports the canonical INBOUND/OUTBOUND shape", () => {
    expect(Array.isArray(INBOUND)).toBe(true);
    expect(Array.isArray(OUTBOUND)).toBe(true);
    expect(INBOUND.length).toBeGreaterThan(0);
    expect(OUTBOUND.length).toBeGreaterThan(0);
    // Every outbound entry is driven either by a produce() fn or an
    // op_via trigger — never both missing (would be an unreachable frame).
    for (const f of OUTBOUND) {
      expect(typeof f.produce === "function" || typeof f.op_via === "string",
        `OUTBOUND ${f.op} has no produce()/op_via driver`).toBe(true);
    }
  });

  it("matches the sibling extension's golden fixture byte-for-byte", () => {
    if (!fs.existsSync(SIBLING)) {
      // Release CI sets QDISTRO_REQUIRE_SIBLING (to any non-empty value) to
      // make a missing sibling fatal instead of a warn-pass.
      expect(
        Boolean(process.env.QDISTRO_REQUIRE_SIBLING),
        `QDISTRO_REQUIRE_SIBLING is set but the sibling fixture was not found at ` +
        `${SIBLING}; check out qdchrome-extension side-by-side or set ` +
        "$QDISTRO_SIBLING_GOLDEN.",
      ).toBe(false);
      // eslint-disable-next-line no-console
      console.warn(
        `[golden-frames.drift] sibling fixture not found at ${SIBLING}; ` +
        "cross-repo byte-identity NOT verified in this run. Set " +
        "$QDISTRO_SIBLING_GOLDEN or check out qdchrome-extension " +
        "side-by-side to enforce it.",
      );
      return;
    }
    const local = fs.readFileSync(LOCAL);
    const sibling = fs.readFileSync(SIBLING);
    expect(
      local.equals(sibling),
      `golden-frames.js drifted from the sibling copy at ${SIBLING}. ` +
      "Both extensions speak the same bridge protocol; mirror the edit " +
      "into the other repo so the copies stay byte-identical.",
    ).toBe(true);
  });
});
