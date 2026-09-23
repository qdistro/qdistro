// Options page. Persists per-module enabled flags + origin-allowlist
// into browser.storage.local. The background gate (src/gate.js) reads
// these at boot and re-reads on storage.onChanged to gate dispatcher
// ops + background message handling: a disabled module's wire ops are
// refused, and content-script ops are restricted to the listed
// origins. The allowlist is CLOSED BY DEFAULT (opus HIGH J11): an
// empty/unset list denies every page-initiated op; a single `*` entry
// opts in to all origins explicitly.
//
// Storage shape:
//   {
//     modules: { tabs: bool, pwd: bool, ... },
//     origin_allowlist: ["https://...", ...],
//   }
//
// @ts-check
const api = (typeof browser !== "undefined") ? browser : chrome;

const MODULES = [
  "tabs", "pwd", "pageExtract", "cookies", "containers",
  "mpris", "downloads", "notifications", "screenlock",
];

async function load() {
  const cfg = await api.storage.local.get(["modules", "origin_allowlist"]);
  const mods = (cfg && cfg.modules) || {};
  for (const m of MODULES) {
    const el = document.getElementById(`mod-${m}`);
    if (!el) continue;
    if (typeof mods[m] === "boolean") el.checked = mods[m];
  }
  const list = (cfg && cfg.origin_allowlist) || [];
  document.getElementById("origin-allowlist").value = list.join("\n");
}

async function save() {
  const mods = {};
  for (const m of MODULES) {
    const el = document.getElementById(`mod-${m}`);
    mods[m] = !!(el && el.checked);
  }
  const list = document.getElementById("origin-allowlist").value
    .split("\n").map((s) => s.trim()).filter(Boolean);
  await api.storage.local.set({
    modules: mods,
    origin_allowlist: list,
  });
  const el = document.getElementById("saved");
  el.style.display = "inline";
  setTimeout(() => { el.style.display = "none"; }, 1500);
}

document.getElementById("save").addEventListener("click", save);
load();
