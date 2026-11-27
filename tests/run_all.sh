#!/bin/sh
# Runs the whole offline self-test suite for wm_device.py: the Python suite (pure-function logic)
# and the Node suite (the tool-mapping popup's embedded JavaScript). Needs python3 always; the JS
# half needs node - if node isn't installed, this skips it with a warning rather than failing.
#
# Usage: tests/run_all.sh [/path/to/wm_device.py]
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-$HERE/../wm_device.py}"

echo "== Python suite =="
python3 "$HERE/run_suite.py" "$TARGET"
PY_STATUS=$?

echo ""
echo "== JavaScript suite (tool-mapping popup) =="
if command -v node >/dev/null 2>&1; then
  node "$HERE/test_tool_mapping_popup.js" "$TARGET"
  JS_STATUS=$?
else
  echo "node not found on this machine - skipping the JS suite (install Node.js to run it, e.g. via nodejs.org or brew install node)"
  JS_STATUS=0
fi

if [ "$PY_STATUS" -ne 0 ] || [ "$JS_STATUS" -ne 0 ]; then
  echo ""
  echo "SUITE FAILED - do not ship this file yet."
  exit 1
fi
echo ""
echo "SUITE PASSED."
