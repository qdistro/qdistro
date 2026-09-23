# Agents notes — qdfirefox-extension

## Layout

Sibling of [qdchrome-extension](../qdchrome-extension). Same protocol, different host.

```
src/
  api.js          → self.qdistroApi = browser
  port.js         → self.qdistroPort
  dispatcher.js   → self.qdistroDispatcher
  intent.js       → self.qdistroIntent
  background.js   → boot
  modules/*.js    → self.qdistro<Module>
```

All sources are IIFEs attaching exports onto `self`. The Firefox MV3 event page is a flat `background.scripts` list — load order is what manifest.json declares.

## Differences from qdchrome-extension to keep in mind

- **API surface**: `browser.*` is Promise-returning. Module code uses `await api.tabs.query({})` directly — no callback wrappers, no `runtime.lastError` checks. Tests' synthetic `browser` must return Promises (see `tests/helpers.js`).
- **MV target**: MV3 only. There is no MV2 fallback path here. If you find yourself adding `try { importScripts(...) } catch { ... }`, stop — that's the wrong repo.
- **Containers**: `src/modules/containers.js` is Firefox-only. It uses `browser.contextualIdentities` which has no Chromium analogue.
- **Cookies**: callers can pass `cookieStoreId` for container scoping and `firstPartyDomain: null` is explicit in `getAll` queries.
- **runtime.onMessage**: Firefox supports returning a Promise from the listener — see `src/background.js`. Don't port back to the chrome.* `return true; sendResponse(...)` shape.

## Running tests in the VM

Per qdistro convention, integration tests run in the bats VM, not on the host. Unit tests (vitest) run on the host:

```bash
npm test                   # host, fast
just test-vm               # in the libvirt template (not yet wired)
```

## Build outputs

```bash
npm run build
# dist/firefox/        unpacked, load via about:debugging
# dist/firefox.xpi     packed, submit to AMO or install signed
```

## Native-host install

`scripts/install-native-host.sh` writes the manifest to `~/.mozilla/native-messaging-hosts/qdistro.json` (user) or `/usr/lib(64)/mozilla/native-messaging-hosts/qdistro.json` (`--system`). The extension ID is `qdistro-firefox@qdistro.local` — keep that in sync with `manifest.json` `browser_specific_settings.gecko.id` if it ever changes.

## Writing tests (read before you touch `tests/`)

This is a JS WebExtension, not pytest. qci runs this repo through its
`npm` step, which is `npm test && npm run build` — i.e.
`vitest run` (see `package.json` `scripts.test`) followed by
`bash scripts/build-extension.sh` (`scripts.build`). A test edit that
fails either half breaks the gate. The qdistro pytest
`@pytest.mark.cheat_aware` marker is **N/A here** — there is no pytest in
this repo — but the discipline behind it is not: state what each test
protects.

### Golden rule: never reduce coverage, never weaken an assertion

New test work is **additive**. Do not delete a test, delete an `expect`,
loosen a matcher (`toEqual` → `toContain`, exact string → loose regex,
`toThrow(/intent_no_session/)` → bare `toThrow()`), widen an arg/scope
comparison, or flip an expected rejection into an expected resolve. If an
existing test looks wrong, **flag it in your report and leave it** — a
human decides whether the test or the source is at fault. Silently
"fixing" a test so it passes is a coverage regression, not a fix
(qdistro `tests/AGENTS.md`).

### Layout

- `tests/*.test.js` — vitest unit specs, one per `src/` module
  (`intent.test.js`, `dispatcher.test.js`, `port.test.js`,
  `cookies.test.js`, `containers.test.js`, `manifest.test.js`, the
  `*-content.test.js` content-script specs, …). No jsdom-heavy setup; run
  fast on the host.
- `tests/helpers.js` — `loadExtension()` / `loadWithBackground()` eval the
  IIFE sources into a synthetic `self`, and `makeFakeBrowser()` supplies a
  Promise-returning `browser.*`. Reuse these; do not hand-roll a second
  fake-`browser` per file or stub `connectNative` ad hoc — extend
  `helpers.js` so every spec shares one fixture.
- `tests/integration/firefox-gui/` — visual GUI scenarios (`NN-*.md`) run
  in the VM against a stub native host; the load-bearing assertion there
  is journal lines, not pixels. Read that directory's `AGENTS.md` before
  authoring a scenario.

### Anti-cheat rules (JS phrasing)

These mirror `qdistro/ci/prompts/anti-cheat-guidance.md`. A green
`vitest run` only counts if the green is earned.

- **No `.skip` / `xit` / `it.skip` / `describe.skip` to dodge a failure,
  and no `it.only` / `describe.only` left in** — `.only` silently hides
  every other test in the file from the run. A skip is not a pass; it is
  an admission the assertion did not run.
- **Do not delete or comment out an `expect`** to get to green.
- **Do not bump a timeout** (`{ timeout }`, `vi.setConfig`,
  `vi.advanceTimersByTime` fudging) to mask flakiness. A test that needs a
  longer wait is usually telling you something never connected or never
  resolved — diagnose the port/dispatcher/Promise instead of padding the
  deadline.
- **No fail → skip.** Do not convert a failing assertion into a
  conditional skip or an early `return`.
- **Do not change a test's expected behavior without a source-code
  justification.** If `src/` changed and the spec must follow, the diff
  must change `src/` too, and your report must say which source change
  forced it. A test-only edit is a coverage regression by default.

### Evidence on failure

Every `expect` must make its evidence visible on the failing path. Prefer
matchers that print expected-vs-actual (`toEqual`, `toMatchObject`,
`rejects.toThrow(/pattern/)`) over a bare `expect(x).toBe(true)` that only
says "false". Assert on the concrete wire shape — the `op`, the
`request_id` correlation, the canonical token fields — so a failure shows
*what* diverged, not just *that* it did. State what each spec `ensures:`
(the user-visible capability it protects), the same way qdistro bats and
GUI assertions do.

### Bridge / permission boundary is security-critical

`intent.js`, `dispatcher.js`, `port.js`, `background.js` and their specs
guard the wire to the qdbrowser native bridge: intent-token HMAC shape and
TTL, `mint()` refusing to issue before handshake (`intent_no_session`),
`request_id` reply correlation, the `runtime.onMessage` sender/permission
checks, and per-op dispatch. The bridge's `verify_intent_token`
(`qdistro/browser_bridge/qdistro_browser_bridge.py`) is the authoritative
spec these track. **Do not weaken these.** Treat them as the
`cheat_aware`-equivalent assertions: in your report, name what each
protects and the consequence of a silent regression (a forged or
over-broad intent token reaching the bridge, a cross-op reply mismatch).
Tightening to match a verified bridge change is fine — and must cite the
bridge change.

### No test screenshots or golden images in the repo

Do not commit PNG/JPEG screenshots or baked-in golden images for tests.
GUI-scenario screenshots are captured at runtime into the qci run
directory, never committed; a scenario describes what to capture and
assert. (The shipped extension icon `icons/icon-48.png` is a product
asset, not a test artifact, and is exempt; `dist/` and `coverage/` are
git-ignored.)

## What this repo does NOT contain

- The bridge daemon itself — lives in qdistro repo (`qdistro-browser-bridge` entry point).
- The qdbrowser Qt browser — lives in qdbrowser.
- Chromium-specific code or MV2 fallback — those live in qdchrome-extension.
