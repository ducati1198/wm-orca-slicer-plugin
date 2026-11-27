// Offline self-test for the "WM Device: tool mapping" popup's embedded JavaScript (the code that
// runs inside the actual popup window, not the Python side). Extracts the live <script> block
// straight from wm_device.py's POPUP template (so it always tests the REAL code, never a copy
// that can drift out of sync) and exercises it with fake DOM/localStorage/orca stubs.
//
// Usage: node tests/test_tool_mapping_popup.js [/path/to/wm_device.py]
// Requires: node (no npm packages). Exits 1 and prints which check failed, 0 if all pass.
"use strict";
const fs = require("fs");
const path = require("path");

const target = process.argv[2] || path.join(__dirname, "..", "wm_device.py");
const src = fs.readFileSync(target, "utf8");

const popupMatch = /POPUP = r"""([\s\S]*?)"""/.exec(src);
if (!popupMatch) { console.error("FAIL: could not find POPUP = r\"\"\" ... \"\"\" in " + target); process.exit(1); }
const html = popupMatch[1];
const scriptMatch = /<script>([\s\S]*?)<\/script>/.exec(html);
if (!scriptMatch) { console.error("FAIL: no <script> block found inside POPUP"); process.exit(1); }
const scriptSrc = scriptMatch[1];

let failures = 0;
function check(name, cond, detail) {
  const ok = !!cond;
  console.log((ok ? "[PASS] " : "[FAIL] ") + name + (!ok && detail !== undefined ? "  ->  " + JSON.stringify(detail) : ""));
  if (!ok) failures++;
}

function runScenario(fils, headsMat) {
  // Fresh fake DOM/localStorage/orca per scenario so scenarios can't leak into each other.
  const elements = {};
  function make(id) { return elements[id] || (elements[id] = { innerHTML: "", textContent: "", style: {}, classList: { toggle(){} } }); }
  global.document = { getElementById: (id) => make(id), body: {} };
  global.localStorage = { getItem(){return null}, setItem(){} };
  let submitted = null;
  global.orca = { submit: (p) => { submitted = p; } };

  global.__DATA__ = {
    file: "test.gcode", host: "1.2.3.4", note: "",
    heads: headsMat, cur: [0,1,2,3], map: [0,1,2,3], mem: null,
    fils: fils,
  };
  global.__PAL__ = [["White","#FFFFFF"],["Black","#000000"]];

  const wrapped = scriptSrc
    .replace(/__DATA__/g, "JSON.parse(JSON.stringify(__DATA__))")
    .replace(/__PAL__/g, "JSON.parse(JSON.stringify(__PAL__))");
  const vm = require("vm");
  const ctx = vm.createContext({ document: global.document, localStorage: global.localStorage, orca: global.orca, console, __DATA__: global.__DATA__, __PAL__: global.__PAL__ });
  vm.runInContext(wrapped, ctx);
  return { ctx, make, getSubmitted: () => submitted };
}

// --- Scenario 1: contiguous used slots (the user's reported case: T0+T1 used, T2/T3 not) -----
{
  const heads = [
    {i:0, colour:"#111111", mat:"PA",  loaded:true},
    {i:1, colour:"#eeeeee", mat:"ABS", loaded:true},
    {i:2, colour:"#ff0000", mat:"PLA", loaded:true},
    {i:3, colour:"#00ff00", mat:"PLA", loaded:true},
  ];
  const fils = [
    {index:0, colour:"#111111", type:"PA",  name:"", used:true},
    {index:1, colour:"#eeeeee", type:"ABS", name:"", used:true},
    {index:2, colour:"#ff8800", type:"PLA", name:"", used:false},
    {index:3, colour:"#8800ff", type:"PLA", name:"", used:false},
  ];
  const { ctx, make } = runScenario(fils, heads);
  check("contiguous case: subtitle shows 2 filaments, not 4",
        make("sub").textContent.indexOf("2 filament") >= 0, make("sub").textContent);
  check("contiguous case: only 2 rows rendered",
        (make("rows").innerHTML.match(/class="r/g) || []).length === 2);
  check("contiguous case: no warning mentions the hidden PLA rows",
        make("warn").innerHTML.indexOf("Filament 3") === -1 && make("warn").innerHTML.indexOf("Filament 4") === -1,
        make("warn").innerHTML);
}

// --- Scenario 2: NON-contiguous used slots (T0+T2 used, T1/T3 not) - the case that would break
// if unused filaments were ever DROPPED from the array instead of just hidden -------------------
{
  const heads = [
    {i:0, colour:"#111111", mat:"PA",  loaded:true},
    {i:1, colour:"#eeeeee", mat:"ABS", loaded:true},
    {i:2, colour:"#ff0000", mat:"PETG",loaded:true},
    {i:3, colour:"#00ff00", mat:"PLA", loaded:true},
  ];
  const fils = [
    {index:0, colour:"#111111", type:"PA",   name:"", used:true},
    {index:1, colour:"#8800ff", type:"PLA",  name:"", used:false},
    {index:2, colour:"#ff0000", type:"PETG", name:"", used:true},
    {index:3, colour:"#8800ff", type:"PLA",  name:"", used:false},
  ];
  const { ctx, make, getSubmitted } = runScenario(fils, heads);
  check("non-contiguous case: only 2 rows rendered (slots 1 and 3 hidden)",
        (make("rows").innerHTML.match(/class="r/g) || []).length === 2);
  // Deliberately swap T0 and T2 on the two USED rows, then submit, and check the map keeps every
  // SLOT at its own array position (position 0 = slot 0, position 2 = slot 2) - this is exactly
  // the indexing bug a naive "drop unused entries" implementation would introduce.
  ctx.pick(0, 2); ctx.pick(2, 0);
  ctx.done("upload");            // first press: shows the mismatch confirmation, doesn't submit yet
  ctx.done("upload");            // second press: confirms and submits
  const sub = getSubmitted();
  check("non-contiguous case: submitted map keeps slot 0 and slot 2 at their own positions",
        sub && sub.map[0] === 2 && sub.map[2] === 0, sub);
}

// --- Scenario 3: nothing marked unused (today's old behaviour, e.g. ferr was set) --------------
{
  const heads = [
    {i:0, colour:"#111111", mat:"PA",  loaded:true},
    {i:1, colour:"#eeeeee", mat:"ABS", loaded:true},
  ];
  const fils = [
    {index:0, colour:"#111111", type:"PA",  name:""},   // no 'used' key at all
    {index:1, colour:"#eeeeee", type:"ABS", name:""},
  ];
  const { make } = runScenario(fils, heads);
  check("no 'used' key present: every row still shows (fails open)",
        (make("rows").innerHTML.match(/class="r/g) || []).length === 2);
}

console.log();
console.log(failures === 0 ? "ALL JS CHECKS PASSED" : failures + " JS CHECK(S) FAILED");
process.exit(failures === 0 ? 0 : 1);
