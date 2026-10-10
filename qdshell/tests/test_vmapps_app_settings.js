// Source invariants for the tier-5 VM-app launcher secondary action and
// the per-app settings navigation it rides on.
//
// Pins the identity contract end to end: the same catalogue appId that
// drives --policy-key and the per-app VM name also addresses the app's
// override row in Settings → "Sandboxed VM apps", and the navigation
// never goes through display strings or sidebar ordering.
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const repo = path.resolve(__dirname, "..");
const provider = fs.readFileSync(
    path.join(repo, "Modules/Panels/Launcher/Providers/VMAppsProvider.qml"),
    "utf8");
const service = fs.readFileSync(
    path.join(repo, "Services/Qdistro/VMApps.qml"), "utf8");
const content = fs.readFileSync(
    path.join(repo, "Modules/Panels/Settings/SettingsContent.qml"), "utf8");
const tab = fs.readFileSync(
    path.join(repo, "Modules/Panels/Settings/Tabs/VMApps/VMAppsTab.qml"),
    "utf8");
const panel = fs.readFileSync(
    path.join(repo, "Modules/Panels/Settings/SettingsPanel.qml"), "utf8");
const win = fs.readFileSync(
    path.join(repo, "Modules/Panels/Settings/SettingsPanelWindow.qml"),
    "utf8");

// --- the action itself -------------------------------------------------
// ensures: every VM-app launcher row exposes a secondary action that
//          opens Settings → "Sandboxed VM apps" — the launcher hook is
//          the standard provider.getItemActions surface, not a parallel
//          mechanism
{
    assert.ok(provider.includes("function getItemActions(item)"),
        "VMAppsProvider must expose item actions via getItemActions");
    const fn = provider.slice(
        provider.indexOf("function getItemActions(item)"));
    assert.ok(fn.includes("SettingsPanelService.openToEntry"),
        "the action must navigate through SettingsPanelService.openToEntry");
    assert.ok(fn.includes('"tabId": SettingsPanel.Tab.VMApps'),
        "the action must address the VMApps tab by its stable enum id");
    assert.ok(fn.includes('"labelKey": item.appId'),
        "the action must carry the item's catalogue appId as the row key");
    // ensures: launch behavior is unchanged — the gear is additive and
    //          never fires the spawn path
    assert.ok(!fn.includes("VMApps.launch"),
        "the settings action must not trigger a launch");
}

// --- tab addressing is enum-id based, not display order -----------------
// ensures: moving the VMApps tab in the sidebar cannot misroute the
//          navigation — entries may carry tabId (enum) and the content
//          resolves it to the current model index
{
    const nav = content.slice(
        content.indexOf("function navigateToResult(entry)"),
        content.indexOf("function navigateToTab("));
    assert.ok(nav.includes("entry.tabId !== undefined"),
        "navigateToResult must resolve entry.tabId");
    assert.ok(nav.includes("tabsModel[i].id === entry.tabId"),
        "tabId must be resolved through tabsModel's enum ids");
    for (const src of [panel, win]) {
        assert.ok(
            src.includes("entry.tabId !== undefined")
            || src.includes("requestedEntry.tabId !== undefined"),
            "entry consumers must prefer tabId for requestedTab");
    }
}

// --- row addressing is appId based, not a translated label --------------
// ensures: the highlight pipeline can address a per-app row by its
//          stable appId — display names are not identity
{
    const finder = content.slice(
        content.indexOf("function findAndHighlightWidget(item, labelKey)"),
        content.indexOf("// Recursively search children"));
    assert.ok(finder.includes('item.highlightKey === labelKey'),
        "findAndHighlightWidget must match an item's highlightKey");
    assert.ok(tab.includes("highlightKey: appRow.appId"),
        "VMAppsTab rows must expose their appId as highlightKey");
}

// --- deterministic per-app VM name (relaunch reuse) ---------------------
// ensures: every launch of the same app lands on the same domain — the
//          name is a pure function of appId with no random component, so
//          the wrapper's hold-lock protocol and the silo tag share one
//          identity
{
    const fn = service.slice(
        service.indexOf("function _vmNameForApp(appId)"),
        service.indexOf("function rebuild()"));
    assert.ok(fn.length > 0, "_vmNameForApp must exist in VMApps.qml");
    assert.ok(!fn.includes("Math.random"),
        "the VM name must not contain random components");
    assert.ok(fn.includes('"t5-" +'),
        "the VM name keeps the t5- prefix convention");
    assert.ok(service.includes("root._vmNameForApp(row.appId)"),
        "launch must use the deterministic per-app name");
    assert.ok(!service.includes("_generateVmName"),
        "the random per-launch name generator must be gone");
    // ensures: launch still passes the stable appId through --policy-key
    assert.ok(service.includes('"--policy-key", row.appId'),
        "launch must keep passing appId as the policy key");
}

// --- collision-freedom is proven behaviorally (sol r1/r2 P1) ------------
// ensures: two DISTINCT catalogue appIds can never share a domain — a
//          name collision would run the second app inside the first
//          app's guest, breaking per-app isolation. The real product
//          functions are evaluated (they are plain JS), not re-derived.
{
    const fnSrc = service.slice(
        service.indexOf("function _appIdHash(appId)"),
        service.indexOf("function rebuild()"));
    const make = new Function("Logger",
        fnSrc + "\nreturn { _vmNameForApp, _dropDomainCollisions };");
    const { _vmNameForApp, _dropDomainCollisions } =
        make({ e: () => {}, w: () => {} });
    assert.strictEqual(typeof _vmNameForApp, "function");
    assert.strictEqual(typeof _dropDomainCollisions, "function");

    // ensures: relaunches of the SAME app still converge on one domain
    assert.strictEqual(_vmNameForApp("tier5/kate"), _vmNameForApp("tier5/kate"),
        "the name must be deterministic per appId");
    // ensures: ids that sanitize alike do not merge into one guest
    assert.notStrictEqual(_vmNameForApp("tier5/foo.bar"),
        _vmNameForApp("tier5/foo-bar"),
        "appIds differing only in punctuation must name different domains");
    // ensures: ids that truncate alike do not merge either — the hash
    //          must cover the FULL appId, not the truncated stub
    const longA = "tier5/" + "a".repeat(60) + ".one";
    const longB = "tier5/" + "a".repeat(60) + ".two";
    assert.notStrictEqual(_vmNameForApp(longA), _vmNameForApp(longB),
        "appIds sharing the 60-char prefix must name different domains");
    // ensures: the sol r2 crafted pair — an actual FNV-1a-32 collision —
    //          no longer maps to one domain under the widened hash
    const cA = "tier5/" + "a".repeat(60) + "n401yffjhjcw";
    const cB = "tier5/" + "a".repeat(60) + "da3or7cef5wj";
    assert.notStrictEqual(_vmNameForApp(cA), _vmNameForApp(cB),
        "the r2 crafted collision pair must name different domains");
    // ensures: names stay inside the wrapper's validation budget
    for (const id of ["tier5/kate", longA, "weird app (x)!.desktop", "x"]) {
        const n = _vmNameForApp(id);
        assert.match(n, /^t5-[a-zA-Z0-9_-]+-[0-9a-f]{16}$/,
            `name '${n}' must be t5-<stub>-<16 hex chars>`);
        assert.ok(n.length <= 63,
            `name '${n}' exceeds the wrapper's 63-char budget`);
    }

    // ensures: even if a collision DOES occur (any hash can be forced),
    //          the catalogue-load guard drops both colliders instead of
    //          letting one app ride in another's guest — injected nameFn
    //          forces the collision that proves the drop is fail-closed
    const forced = _dropDomainCollisions(
        [{appId: "tier5/firefox"}, {appId: "tier5/evil"}],
        () => "t5-same");
    assert.deepStrictEqual(forced, [],
        "both rows sharing one domain name must be dropped");
    // ensures: a duplicate row for the SAME appId is not a collision —
    //          identical app, identical domain, legitimately adopted
    const dupe = _dropDomainCollisions(
        [{appId: "tier5/kate"}, {appId: "tier5/kate"}],
        () => "t5-same");
    assert.strictEqual(dupe.length, 2,
        "same appId sharing one domain is adoption, not a collision");
    // ensures: ordinary distinct names pass through untouched
    const okRows = [{appId: "tier5/kate"}, {appId: "tier5/firefox"}];
    assert.strictEqual(
        _dropDomainCollisions(okRows, _vmNameForApp).length, 2,
        "distinct names must all remain launchable");
    // ensures: the guard actually runs at catalogue load and can leave
    //          the foot fallback when nothing survives
    assert.ok(service.includes("root._dropDomainCollisions(rows)"),
        "_loadManifest must run the collision guard");
    assert.ok(service.includes("no launchable apps"),
        "an all-colliding catalogue must fall back loudly");
}

// --- stale rows cannot reach a domain (sol r3) --------------------------
// ensures: launch() verifies the row's appId is still in the CURRENT
//          catalogue BEFORE deriving the name — a provider closure that
//          outlives a catalogue swap can't adopt a guest it no longer
//          owns. The wrapper enforces the same via the domain's bound
//          appId; this is the shell-side belt.
{
    const launch = service.slice(
        service.indexOf("function launch(row)"),
        service.indexOf("// ---- placeholders"));
    assert.ok(launch.indexOf("root.catalogue") > -1
        && launch.indexOf("root.catalogue") < launch.indexOf("_vmNameForApp"),
        "launch must check catalogue membership before naming the domain");
    assert.ok(launch.includes("not in current catalogue"),
        "launch must reject appIds dropped from the catalogue");
}

// --- out-of-charset appIds never enter the catalogue (sol r4) -----------
// ensures: an appId the wrapper cannot honor as --policy-key is dropped
//          at load — a malformed manifest row must not reach launch and
//          die at the wrapper, or alias another app's binding. The real
//          _validRow is evaluated, not re-derived.
{
    const src = service.slice(
        service.indexOf("function _validRow(row)"),
        service.indexOf("function _loadManifest()"));
    const make = new Function(src + "\nreturn _validRow;");
    const _validRow = make();
    const good = {
        appId: "tier5/org.kde.kate", name: "Kate",
        execArgv: '["kate"]', iconName: "kate",
    };
    assert.strictEqual(_validRow(good), true,
        "a well-formed catalogue row must validate");
    for (const bad of ["tier5/my app", "evil+tool", "app\tid",
                       "-leading-dash", "tier5/" + "x".repeat(200), ""]) {
        assert.strictEqual(_validRow({...good, appId: bad}), false,
            `appId '${bad}' outside the policy-key charset must drop`);
    }
}

// --- the vmName bypass stays closed (sol r2 P1) -------------------------
// ensures: launch() cannot take a manifest-supplied domain name that
//          would skip the collision guard — appId is the only identity
{
    const launch = service.slice(
        service.indexOf("function launch(row)"),
        service.indexOf("// ---- placeholders"));
    assert.ok(!launch.includes("row.vmName"),
        "launch must not accept a manifest-supplied domain name");
}

console.log(
    "vmapps-app-settings: launcher action, enum-id tab nav, appId " +
    "highlight, deterministic VM name invariants passed");
