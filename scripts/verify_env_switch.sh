#!/usr/bin/env bash
# Acceptance checks for issue #116: the VTAPI_TESTS_MANUAL switch does one thing only.
#
#   1) `pytest -q tests` exits 0 under every spelling of the switch.
#   2) Only "1" enables the manual gate; the truth test has a single definition
#      shared by tests/conftest.py and tests/manual/conftest.py.
set -uo pipefail

cd "$(dirname "$0")/.."

MANUAL_FILE="tests/manual/test_wechat_real.py"
# Snapshot of config/ taken before anything runs, so section 5 can prove this
# script changed nothing -- a pre-existing real config.jsonc must not be
# reported as "left behind", and must never be deleted by this script.
config_listing_before="$(ls -A config)"
failures=0

run() {
  local value="$1"; shift
  if [ "$value" = "__unset__" ]; then
    env -u VTAPI_TESTS_MANUAL "$@"
  else
    VTAPI_TESTS_MANUAL="$value" "$@"
  fi
}

report() {
  local label="$1" rc="$2"
  if [ "$rc" -eq 0 ]; then
    echo "PASS  ${label}"
  else
    echo "FAIL  ${label} (exit=${rc})"
    failures=$((failures + 1))
  fi
}

echo "== 1. full suite is green under every value of VTAPI_TESTS_MANUAL =="
for value in __unset__ 1 true yes TRUE; do
  run "$value" uv run --frozen pytest -q tests >/dev/null 2>&1
  report "pytest -q tests with VTAPI_TESTS_MANUAL=${value}" "$?"
done

echo
echo "== 2. manual gate recognises only '1' =="
for value in __unset__ true yes TRUE 0; do
  out="$(run "$value" uv run --frozen pytest "$MANUAL_FILE" -rs 2>&1)"
  rc=$?
  if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q 'SKIPPED'; then
    report "manual tests skipped with VTAPI_TESTS_MANUAL=${value}" 0
  else
    report "manual tests skipped with VTAPI_TESTS_MANUAL=${value} (rc=${rc})" 1
  fi
done

out="$(VTAPI_TESTS_MANUAL=1 uv run --frozen pytest "$MANUAL_FILE" --collect-only -q 2>&1)"
rc=$?
collected="$(printf '%s' "$out" | awk -v f="$MANUAL_FILE" '$0 ~ "^" f ": [0-9]+$" {split($0, a, ": "); print a[2]; exit}')"
if [ "$rc" -eq 0 ] && [ -n "$collected" ] && [ "$collected" -ge 1 ] && ! printf '%s' "$out" | grep -qi 'skipped'; then
  report "manual tests collected with VTAPI_TESTS_MANUAL=1" 0
else
  report "manual tests collected with VTAPI_TESTS_MANUAL=1 (rc=${rc})" 1
fi

echo
echo "== 3. single truth definition =="
uv run --frozen pytest -q tests/unit/test_manual_test_gate.py >/dev/null 2>&1
report "behaviour tests for the switch" "$?"

echo
echo "== 4. suite is green in BOTH config environments =="
# (a) no config/config.jsonc (CI norm)
if [ -e config/config.jsonc ]; then
  echo "FAIL  (a) cannot run the no-config case: config/config.jsonc exists (move it aside first)"
  failures=$((failures + 1))
else
  uv run --frozen pytest tests -q --tb=short >/dev/null 2>&1
  report "(a) pytest tests with no config/config.jsonc" "$?"
fi

# (b) a config/config.jsonc exists on disk (developer-machine norm). Built from
# config.example.jsonc with a sentinel api_key -- no real credentials here. The
# gate must ignore it and inject the placeholder anyway (9371d52d); a copy of the
# example would be indistinguishable from the placeholder, the sentinel is not.
CONFIG_PATH="config/config.jsonc"
if [ -e "$CONFIG_PATH" ]; then
  echo "FAIL  (b) cannot create $CONFIG_PATH: a file is already there (refusing to touch it)"
  failures=$((failures + 1))
else
  sed 's/your-tikhub-api-key-here/sentinel-on-disk-config-must-not-win/' \
    config/config.example.jsonc > "$CONFIG_PATH"
  uv run --frozen pytest tests -q --tb=short >/dev/null 2>&1
  report "(b) pytest tests with a config/config.jsonc on disk" "$?"
  rm -f "$CONFIG_PATH"
fi

echo
echo "== 5. the script must not have changed config/ =="
after_listing="$(ls -A config)"
if [ "$after_listing" = "$config_listing_before" ]; then
  report "config/ listing unchanged by this script" 0
else
  report "config/ listing changed by this script (before: ${config_listing_before} | after: ${after_listing})" 1
fi

echo
if [ "$failures" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "${failures} CHECK(S) FAILED"
fi
exit "$failures"