# Agent instructions for writing qdchrome-extension tests

You are adding or changing tests in this repo (the qdistro native-messaging
WebExtension, MV3 + MV2). Read this before you touch anything under `tests/`.
This is a **JS/TS** project run by **vitest**, not pytest — the qdistro
`@pytest.mark.cheat_aware` marker does **not** apply here (see the note at the
end). The same *discipline* still does.

## Golden rule: never reduce coverage, never weaken an assertion

New test work is **additive**. Do not delete a test, delete an `expect(...)`,
loosen a matcher (`toEqual` → `toMatch` → `toBeTruthy`, exact object →
partial), widen an argument/scope comparison, or turn a real failure into a
pass. If an existing test looks wrong, **flag it in your report and leave it**
— a human decides whether the test or the product is at fault. Silently
"fixing" a test by making it pass is a coverage regression, not a fix.

## Test runner and layout

- Runner: **vitest** (`devDependencies` in `package.json`). Config is vitest's
  defaults — there is no `vitest.config.*`; jsdom is available as a devDep for
  DOM/content-script tests.
- Layout: flat `tests/*.test.js`, one file per surface (e.g.
  `tests/port.test.js`, `tests/dispatcher.test.js`, `tests/background.test.js`,
  `tests/manifest.test.js`). Shared fixtures live in `tests/helpers.js`
  (`loadExtension`, `makeFakePort`, `makeFakeChrome`); use these instead of
  hand-rolling a fake `chrome`/port per file.
- Scripts (`package.json`):
  - `npm test` → `vitest run` (single non-watch pass).
  - `npm run test:watch` → `vitest` (watch mode, for local iteration only).
  - `npm run build` → `bash scripts/build-extension.sh`.
- **qci runs `npm test && npm run build`.** Both must be green. A build that
  no longer compiles the code your test exercises is also a regression — do
  not make a test pass by gutting what `build-extension.sh` packages.

## Anti-cheat rules (phrased for JS)

A green run only counts if the green is earned. Do none of the following:

1. **No skipping to dodge a failure.** Do not add `.skip` / `describe.skip` /
   `it.skip` / `xit` / `xdescribe`, and do not narrow a suite to `.only` /
   `it.only` / `describe.only` to stop a sibling test from running. A skipped
   or excluded test is an admission the assertion did not run — it is not green.
2. **No deleting or weakening assertions.** Do not remove an `expect`, swap a
   strict matcher for a loose one, or change an expected *deny/reject* into an
   expected *allow/resolve* to get past a red gate.
3. **No bumping timeouts to mask flakiness.** Do not raise `vi.advanceTimersByTime`,
   test `timeout`, or backoff/heartbeat deadlines to paper over a test that
   started slowly or never settled. Diagnose the real cause (use fake timers
   deterministically, as `port.test.js` does) instead of padding the deadline.
4. **No turning a fail into a skip/warning.** If the product changed and a test
   must follow, the diff must also change product code, and your report must
   say which product change forced the test change. A test edit with no
   corresponding source edit is, by default, a coverage regression.

## The messaging / permission boundary is security-critical

This extension is the qdbrowser bridge: it talks to the native host over a
`nativeMessaging` port (`src/port.js`) and routes ops through the dispatcher
(`src/dispatcher.js`), and its manifests declare `nativeMessaging` plus
`host_permissions`. Tests that guard this boundary are **security-critical**
and must not be weakened:

- request_id correlation, reply-shape, unknown-op rejection, and timeout
  handling in `dispatcher.test.js`;
- port connect/heartbeat-ack/disconnect/reconnect lifecycle in `port.test.js`;
- the declared `permissions` / `host_permissions` asserted in
  `manifest.test.js`.

Treat a loosened op-routing match, a dropped unknown-op rejection, a widened
permission set, or a heartbeat/timeout deadline bumped to hide a hang the same
way qdistro treats a broadened qsu approval: a denial or boundary that stops
being enforced is a coverage regression, not a fix. If one of these genuinely
needs to change, justify it against a product change in your report.

## Evidence on failure

Every assertion must make its evidence visible on the failing path. Prefer
specific matchers (`toEqual` on the exact message object, `toMatch` on the
exact string) over `toBeTruthy`, so vitest's diff shows expected vs. actual.
For an asynchronous expectation, assert on the resolved/rejected value, not
just that a promise settled. A bare green is not a result — the failing run
must show what was expected and what was observed.

## Declare what each test protects

The pytest `cheat_aware` marker is **N/A** in this JS repo — there is no such
marker and you should not invent one. But the discipline behind it applies:
state, in the `describe`/`it` name or a one-line comment, the user-visible
capability each test protects (the existing files do this — see the header
comments in `port.test.js` and `dispatcher.test.js`). Examples:

- `ensures: an unknown op from the native host is rejected, not dispatched`
- `ensures: the manifest does not silently gain a broader host permission`
- `ensures: a dropped port reconnects, so the bridge stays available`

If you cannot state what a test ensures, you do not yet understand what you
are protecting — find out before you weaken or delete it.

## Constraints

- **Do not commit PNG or other image files to the repo.** Tests assert on
  data and message shapes, not on baked-in golden images.
- Docs/tests changes here are additive. Do not modify `package.json` scripts,
  the build script, or `src/` to make a test pass.
