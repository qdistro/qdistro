// scripts/install-native-host.sh — bridge-path resolution order.
//
// The script writes the native-messaging host manifest whose "path" the
// browser execs. It used to resolve the bridge ONLY via
// `command -v qdistro-browser-bridge` — a binary no qdistro install ever
// puts on PATH (the installed CLI is `qdistro-browser-install`; the host
// itself lives at /usr/lib/qdistro/browser-bridge). So on every packaged
// install the script exited 1 unless the caller happened to set
// QDISTRO_BRIDGE_PATH by hand. R4 fixed the order to:
//
//   $QDISTRO_BRIDGE_PATH  ->  /usr/lib/qdistro/browser-bridge  ->  $PATH
//
// ensures: the packaged install path is preferred over a dev-only PATH
// lookup, and an explicit override still wins.
import { describe, it, expect } from "vitest";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT = path.join(__dirname, "..", "scripts", "install-native-host.sh");
const PACKAGED = "/usr/lib/qdistro/browser-bridge";

// Run `install-native-host.sh --print` with a synthetic environment and
// return {status, stdout, stderr}. PATH is replaced wholesale so the test
// controls whether a `qdistro-browser-bridge` is discoverable.
function runPrint({ env = {}, pathDir = null } = {}) {
  const base = {
    ...process.env,
    // Keep the system dirs (bash/coreutils must resolve); the fixture dir is
    // prepended only when the test wants a discoverable qdistro-browser-bridge.
    PATH: pathDir ? `${pathDir}:/usr/bin:/bin` : "/usr/bin:/bin",
  };
  try {
    const stdout = execFileSync("bash", [SCRIPT, "--print"], {
      env: { ...base, ...env },
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
    return { status: 0, stdout };
  } catch (e) {
    return { status: e.status ?? 1, stdout: e.stdout || "", stderr: e.stderr || "" };
  }
}

function manifestPath(stdout) {
  return JSON.parse(stdout).path;
}

describe("install-native-host.sh — bridge path resolution", () => {
  it("an explicit QDISTRO_BRIDGE_PATH wins", () => {
    const r = runPrint({ env: { QDISTRO_BRIDGE_PATH: "/opt/custom/bridge" } });
    expect(r.status).toBe(0);
    expect(manifestPath(r.stdout)).toBe("/opt/custom/bridge");
  });

  it("falls back to a PATH-resolved qdistro-browser-bridge (dev tree)", () => {
    // Only meaningful when the packaged path is absent on this host; if a
    // real qdistro install is present it legitimately wins (asserted below).
    if (fs.existsSync(PACKAGED)) {
      return; // a real install is present: the packaged path wins (next test)
    }
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "qdnh-"));
    const fake = path.join(dir, "qdistro-browser-bridge");
    fs.writeFileSync(fake, "#!/bin/sh\nexit 0\n");
    fs.chmodSync(fake, 0o755);
    const r = runPrint({ env: { QDISTRO_BRIDGE_PATH: "" }, pathDir: dir });
    expect(r.status).toBe(0);
    expect(manifestPath(r.stdout)).toBe(fake);
    fs.rmSync(dir, { recursive: true, force: true });
  });

  // The precedence branch itself, exercised on ANY host: the script reads
  // the packaged location from $QDISTRO_DEFAULT_BRIDGE_PATH (test hook,
  // defaulting to /usr/lib/qdistro/browser-bridge), so a synthetic
  // "packaged" bridge can be placed next to a PATH-discoverable one.
  it("prefers the packaged bridge path over a PATH-resolved one", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "qdnh-"));
    const packaged = path.join(dir, "packaged-browser-bridge");
    const onPath = path.join(dir, "qdistro-browser-bridge");
    for (const f of [packaged, onPath]) {
      fs.writeFileSync(f, "#!/bin/sh\nexit 0\n");
      fs.chmodSync(f, 0o755);
    }
    const r = runPrint({
      env: { QDISTRO_BRIDGE_PATH: "", QDISTRO_DEFAULT_BRIDGE_PATH: packaged },
      pathDir: dir,
    });
    expect(r.status).toBe(0);
    expect(manifestPath(r.stdout)).toBe(packaged);
    fs.rmSync(dir, { recursive: true, force: true });
  });

  it("fails with an actionable message when nothing resolves", () => {
    // Point the packaged location at a path that cannot exist, so the case
    // is exercised even on a host with a real qdistro install.
    const r = runPrint({
      env: {
        QDISTRO_BRIDGE_PATH: "",
        QDISTRO_DEFAULT_BRIDGE_PATH: "/nonexistent/qdistro/browser-bridge",
      },
    });
    expect(r.status).not.toBe(0);
    expect(r.stderr).toContain("/nonexistent/qdistro/browser-bridge");
  });

  it("authorizes the standalone gecko id", () => {
    const r = runPrint({ env: { QDISTRO_BRIDGE_PATH: "/x/bridge" } });
    const body = JSON.parse(r.stdout);
    expect(body.name).toBe("qdistro");
    expect(body.type).toBe("stdio");
    expect(body.allowed_extensions).toEqual(["qdistro-firefox@qdistro.local"]);
  });
});
