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

console.log(
    "vmapps-app-settings: launcher action, enum-id tab nav, appId " +
    "highlight, deterministic VM name invariants passed");
