// Offline self-test for two small pure helpers in wm_device.py's main page script (PAGE,
// not the tool-mapping POPUP): dryFmt() (B-099: the ACE dryer "Time left" field showed minutes
// mislabelled as hours, because remain_time comes from the ACE driver in SECONDS, not minutes)
// and fileProgress() (B-098: the Printing Progress percentage didn't match Fluidd's on the same
// file, because it preferred display_status.progress instead of Fluidd's own default "File"
// calculation - virtual_sdcard.file_position relative to the file's gcode_start_byte/gcode_end_byte).
//
// Extracts the two function bodies straight from the real wm_device.py (brace-matched, not a
// hand-copied duplicate) and runs them standalone - neither function touches the DOM, so no
// document/localStorage/orca stubs are needed here (unlike test_tool_mapping_popup.js).
//
// Usage: node tests/test_progress_and_dryer.js [/path/to/wm_device.py]
// Requires: node (no npm packages). Exits 1 and prints which check failed, 0 if all pass.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const target = process.argv[2] || path.join(__dirname, "..", "wm_device.py");
const src = fs.readFileSync(target, "utf8");

function extractFunction(name) {
  const marker = "function " + name + "(";
  const start = src.indexOf(marker);
  if (start === -1) { console.error("FAIL: could not find " + marker + " in " + target); process.exit(1); }
  const braceStart = src.indexOf("{", start);
  let depth = 0, i = braceStart;
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) break; }
  }
  return src.slice(start, i + 1);
}

const dryFmtSrc = extractFunction("dryFmt");
const fileProgressSrc = extractFunction("fileProgress");
const dryerDataSrc = extractFunction("dryerData");
const dryMinutesSrc = extractFunction("dryMinutes");
const filamentUsedStrSrc = extractFunction("filamentUsedStr");

const ctx = {};
vm.createContext(ctx);
vm.runInContext(dryFmtSrc + "\n" + fileProgressSrc + "\n" + dryerDataSrc + "\n" + dryMinutesSrc + "\n" + filamentUsedStrSrc, ctx);

let failures = 0;
function check(name, cond, detail) {
  const ok = !!cond;
  console.log((ok ? "[PASS] " : "[FAIL] ") + name + (!ok && detail !== undefined ? "  ->  " + JSON.stringify(detail) : ""));
  if (!ok) failures++;
}

// ---- dryFmt() - B-099 ----------------------------------------------------------------------
check("dryFmt(5400) -> '1h 30min' (90 minutes, the user's reported case)",
  ctx.dryFmt(5400) === "1h 30min", ctx.dryFmt(5400));
check("dryFmt(60) -> '1h 0min' (exactly one hour)",
  ctx.dryFmt(60 * 60) === "1h 0min", ctx.dryFmt(60 * 60));
check("dryFmt(900) -> '15min' (under an hour, no 'h' part)",
  ctx.dryFmt(900) === "15min", ctx.dryFmt(900));
check("dryFmt(0) -> '0min' (not a huge bogus hour count)",
  ctx.dryFmt(0) === "0min", ctx.dryFmt(0));

// ---- fileProgress() - B-098 -----------------------------------------------------------------
// A file with a sizeable OrcaSlicer header/footer: gcode body is bytes 2000-12000 of a 12500-byte
// file. 2900 bytes into the body (fp=4900) should read as file-relative progress, not raw
// file-position ratio (which would read much lower, same bug shape as the user's 6% vs 29%).
const fb = { start: 2000, end: 12000 };
check("fileProgress: 2900 of 10000 body bytes read -> 0.29 (matches Fluidd's 'File' mode)",
  Math.abs(ctx.fileProgress({ file_position: 4900, progress: 4900 / 12500 }, fb) - 0.29) < 1e-9,
  ctx.fileProgress({ file_position: 4900, progress: 4900 / 12500 }, fb));
check("fileProgress: before gcode_start_byte -> 0",
  ctx.fileProgress({ file_position: 500, progress: 0.04 }, fb) === 0);
check("fileProgress: at/after gcode_end_byte -> 1",
  ctx.fileProgress({ file_position: 12500, progress: 1 }, fb) === 1);
check("fileProgress: no fileBytes (metadata missing) falls back to virtual_sdcard.progress, same as Fluidd",
  ctx.fileProgress({ file_position: 4900, progress: 0.392 }, null) === 0.392);
check("fileProgress: no file_position at all falls back to virtual_sdcard.progress",
  ctx.fileProgress({ progress: 0.07 }, fb) === 0.07);

// ---- dryerData() - B-100 --------------------------------------------------------------------
// Some ACE Pro firmware/protocol versions report the block as "dryer_status" instead of "dryer"
// in the raw per-instance status; Klipper's get_status() (what Moonraker, and so this plugin,
// reads) passes that raw key straight through with no normalization, unlike the driver's own
// console command which already checks both. Before this fix, a.dryer was simply undefined on
// those firmwares, so Status/Time left showed "-" even while the dryer was actively running.
check("dryerData: reads 'dryer' when that's the key present",
  JSON.stringify(ctx.dryerData({ dryer: { status: "run", remain_time: 900 } })) === JSON.stringify({ status: "run", remain_time: 900 }));
check("dryerData: falls back to 'dryer_status' when 'dryer' is absent (the user's reported case)",
  JSON.stringify(ctx.dryerData({ dryer_status: { status: "run", remain_time: 900 } })) === JSON.stringify({ status: "run", remain_time: 900 }));
check("dryerData: prefers 'dryer_status' over 'dryer' when both are present (matches the driver's own ACE_STATUS precedence)",
  ctx.dryerData({ dryer: { status: "A" }, dryer_status: { status: "B" } }).status === "B");
check("dryerData: empty object, not undefined, when neither key exists (callers do d.status safely)",
  JSON.stringify(ctx.dryerData({})) === "{}");

// ---- dryMinutes() - the Hour-only duration input (0.29.28 dropped the Minutes box) ------------
check("dryMinutes(4) -> 240 (4 hours)",
  ctx.dryMinutes(4) === 240);
check("dryMinutes(1) -> 60",
  ctx.dryMinutes(1) === 60);
check("dryMinutes('') -> 0 (empty input doesn't throw NaN through)",
  ctx.dryMinutes("") === 0);
check("dryMinutes(12) -> 720 (max hours)",
  ctx.dryMinutes(12) === 720);

// ---- filamentUsedStr() - new Filament field in the Printing Progress panel -------------------
check("filamentUsedStr(2345) -> '2.35 m'",
  ctx.filamentUsedStr(2345) === "2.35 m");
check("filamentUsedStr(null) -> '-'",
  ctx.filamentUsedStr(null) === "-");
check("filamentUsedStr(undefined) -> '-'",
  ctx.filamentUsedStr(undefined) === "-");
check("filamentUsedStr(0) -> '0.00 m' (a real zero, not treated as missing)",
  ctx.filamentUsedStr(0) === "0.00 m");

console.log();
if (failures) {
  console.log(failures + " check(s) FAILED");
  process.exit(1);
}
console.log("ALL PROGRESS/DRYER CHECKS PASSED");
