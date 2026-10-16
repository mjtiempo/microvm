#!/usr/bin/env bash
# Run the unit tests and every acceptance suite in order.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

echo "== unit tests =="
python3 -m unittest tests.unit.test_microvm_control -v

for suite in m0 m1-pool m2-clone m3-backup m4-agent m5-hardening; do
  echo
  echo "== $suite =="
  "tests/accept/$suite.sh"
done

echo
echo "ALL UNIT + ACCEPTANCE SUITES PASSED"
