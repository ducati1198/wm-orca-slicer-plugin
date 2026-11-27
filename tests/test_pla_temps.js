// Offline self-test for the ZRM/ZRT default-temperature table in wm_device.py's main page script:
// the "all PLA types are 220 C" rule (printer-side material table change on both the production
// printer and the test pair, mirrored here). ZRT[i] is the default nozzle temp spMat() pre-fills
// in the Spool editor's Nozzle temp box when the material at ZRM[i] is picked.
//
// Extracts the ZRM and ZRT array literals straight from the real wm_device.py (not a hand-copied
// duplicate) so this fails if the file and the test ever drift apart.
//
// Usage: node tests/test_pla_temps.js [/path/to/wm_device.py]
// Requires: node (no npm packages). Exits 1 and prints which check failed, 0 if all pass.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const target = process.argv[2] || path.join(__dirname, "..", "wm_device.py");
const src = fs.readFileSync(target, "utf8");

function extractArray(varName) {
  const marker = "var " + varName + "=";
  const start = src.indexOf(marker);
  if (start === -1) { console.error("FAIL: could not find " + marker + " in " + target); process.exit(1); }
  const end = src.indexOf(";", start);
  return src.slice(start, end + 1);
}

const ctx = {};
vm.createContext(ctx);
vm.runInContext(extractArray("ZRM") + "\n" + extractArray("ZRT"), ctx);

let failures = 0;
function check(name, cond, detail) {
  const ok = !!cond;
  console.log((ok ? "[PASS] " : "[FAIL] ") + name + (!ok && detail !== undefined ? "  ->  " + JSON.stringify(detail) : ""));
  if (!ok) failures++;
}

check("ZRM and ZRT are the same length (27 materials)",
  ctx.ZRM.length === 27 && ctx.ZRT.length === 27, { zrmLen: ctx.ZRM.length, zrtLen: ctx.ZRT.length });

function tempOf(material) {
  const i = ctx.ZRM.indexOf(material);
  if (i === -1) { console.error("FAIL: material not found in ZRM: " + material); process.exit(1); }
  return ctx.ZRT[i];
}

// All PLA-family materials are 220 C, matching the printer-side material table (Klipper, ACE
// driver and KlipperScreen) on both the production printer and the test pair.
["PLA", "PLA Silk", "PLA Matte", "PLA Metal", "PLA+", "PLA-CF"].forEach(function (m) {
  check("ZRT default for " + m + " is 220", tempOf(m) === 220, tempOf(m));
});

// Everything else keeps its previous default - this rule only touches PLA types.
const unchanged = {
  "ABS": 250, "PETG": 240, "ABS Matte": 250, "PETG Matte": 240, "PVA": 200, "TPE": 225,
  "Marble": 210, "HIPS": 235, "TPU": 225, "PET": 240, "Wood": 200, "ASA": 255,
  "PETG-CF": 250, "PA": 270, "PA-CF": 280, "PC": 270, "ABS-CF": 260, "PA-GF": 280,
  "PC-ABS": 260, "PP": 230, "BVOH": 210
};
Object.keys(unchanged).forEach(function (m) {
  check("ZRT default for " + m + " is unchanged (" + unchanged[m] + ")", tempOf(m) === unchanged[m], tempOf(m));
});

console.log();
if (failures) {
  console.log(failures + " check(s) FAILED");
  process.exit(1);
}
console.log("ALL PLA-TEMPERATURE CHECKS PASSED");
