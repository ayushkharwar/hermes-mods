#!/usr/bin/env bash
# Run every offline test suite. No network, no live API calls.
set -euo pipefail

cd "$(dirname "$0")/.."

fail=0
run() {
  local name="$1"; shift
  if "$@" >/tmp/verify-"${name//\//_}".log 2>&1; then
    printf 'PASS  %s\n' "$name"
  else
    printf 'FAIL  %s   (see /tmp/verify-%s.log)\n' "$name" "${name//\//_}"
    fail=1
  fi
}

run "savvy-progress plugin"  python3 tests/savvy-progress/test_plugin.py
run "savvy-progress widget"  node    tests/savvy-progress/test_widget.mjs
run "savvy-progress integration" python3 tests/savvy-progress/test_integration.py
run "savvy-progress hardening"   python3 tests/savvy-progress/test_hardening.py
run "savvy-progress concurrency" python3 tests/savvy-progress/test_concurrency.py
run "savvy-progress soak"        python3 tests/savvy-progress/test_soak.py
run "filetree-rail plugin"   python3 tests/filetree-rail/test_plugin.py
run "filetree-rail widget"   node    tests/filetree-rail/test_widget.mjs
run "keepwarm capture"       python3 tests/keepwarm/test_capture.py
run "keepwarm ping"          python3 tests/keepwarm/test_ping.py
run "keepwarm stop"          python3 tests/keepwarm/test_stop.py
run "keepwarm truncated"     python3 tests/keepwarm/test_truncated.py

echo
if [ "$fail" -ne 0 ]; then
  echo "verify: FAILED"
  exit 1
fi
echo "verify: all suites passed"
