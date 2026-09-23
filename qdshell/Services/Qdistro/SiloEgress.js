"use strict";

var ACTIVE_STATES = {
  "Active": true,
  "ACTIVE": true,
  "active": true,
};

function parseBusctlListSilos(raw) {
  if (!raw)
    return [];
  var parsed = null;
  try {
    parsed = JSON.parse(String(raw).trim());
  } catch (e) {
    return [];
  }
  if (!parsed || !parsed.data || parsed.data.length < 1)
    return [];
  try {
    var rows = JSON.parse(String(parsed.data[0] || "[]"));
    return Array.isArray(rows) ? rows : [];
  } catch (e) {
    return [];
  }
}

function normaliseEgress(value) {
  if (value === null || value === undefined || value === "")
    return "legacy";
  value = String(value);
  if (value === "none")
    return "none";
  if (value === "direct")
    return "direct";
  if (value.indexOf("wg:") === 0)
    return value;
  return "unknown";
}

function egressLabel(egress) {
  if (egress === "legacy")
    return "host";
  if (egress === "direct")
    return "direct";
  if (egress && egress.indexOf("wg:") === 0)
    return egress.slice(3) || "wg";
  if (egress === "unknown")
    return "unknown";
  return "";
}

function activeEgressRows(rows) {
  var out = [];
  if (!Array.isArray(rows))
    return out;
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i] || {};
    if (!ACTIVE_STATES[String(row.state || "")])
      continue;
    var egress = normaliseEgress(row.egress);
    if (egress === "none")
      continue;
    out.push({
      name: String(row.name || ""),
      uid: Number(row.uid || 0),
      egress: egress,
      label: egressLabel(egress),
    });
  }
  out.sort(function (a, b) {
    return a.name.localeCompare(b.name);
  });
  return out;
}

function summary(rows, limit) {
  var active = activeEgressRows(rows);
  var max = Math.max(1, Number(limit || 2));
  if (active.length === 0)
    return {
      active: false,
      count: 0,
      label: "",
      detail: "",
    };
  var labels = [];
  for (var i = 0; i < Math.min(max, active.length); i++) {
    var r = active[i];
    labels.push(r.name ? (r.name + ":" + r.label) : r.label);
  }
  var extra = active.length - labels.length;
  return {
    active: true,
    count: active.length,
    label: extra > 0 ? labels.join(", ") + " +" + extra : labels.join(", "),
    detail: active.map(function (r) {
      return r.name ? (r.name + ":" + r.label) : r.label;
    }).join(", "),
  };
}

var api = {
  parseBusctlListSilos: parseBusctlListSilos,
  normaliseEgress: normaliseEgress,
  egressLabel: egressLabel,
  activeEgressRows: activeEgressRows,
  summary: summary,
};

if (typeof module !== "undefined")
  module.exports = api;
