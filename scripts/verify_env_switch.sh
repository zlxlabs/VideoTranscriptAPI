#!/usr/bin/env bash
# Acceptance checks for issue #116: the VTAPI_TESTS_MANUAL switch does one thing only.
#
#   1) `pytest -q tests` exits 0 under every spelling of the switch.
#   2) Only "1" enables the manual gate; the truth test has a single definition
#      shared by tests/conftest.py and tests/manual/conftest.py.
set -uo pipefail

cd "$(dirname "$0")/.."

MANUAL_FILE="tests/manual/test_wechat_real.py"
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
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -qE 'test_wechat_real\.py: [1-9]' && ! printf '%s' "$out" | grep -qi 'skipped'; then
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
  echo "SKIP  (a) config/config.jsonc already present"
else
  uv run --frozen pytest tests -q --tb=short >/dev/null 2>&1
  report "(a) pytest tests with no config/config.jsonc" "$?"
fi

# (b) real config/config.jsonc (developer machine norm) -- source it from the
# main checkout when available, since config.jsonc is gitignored per worktree.
# MAIN_REPO may be given explicitly; otherwise it is discovered from git.
MAIN_REPO="${MAIN_REPO:-}"
if [ -z "$MAIN_REPO" ]; then
  MAIN_REPO="$(git worktree list --porcelain 2>/dev/null |
    awk '/^worktree /{print $2}' |
    while read -r wt; do
      if [ "$wt" != "$(pwd)" ] && [ -f "$wt/config/config.jsonc" ]; then
        printf '%s\n' "$wt"; break
      fi
    done)"
fi
if [ -n "$MAIN_REPO" ] && [ -f "$MAIN_REPO/config/config.jsonc" ]; then
  cp "$MAIN_REPO/config/config.jsonc" config/config.jsonc
  uv run --frozen pytest tests -q --tb=short >/dev/null 2>&1
  report "(b) pytest tests with a real config/config.jsonc" "$?"
  rm -f config/config.jsonc
else
  echo "SKIP  (b) no real config.jsonc available in another worktree (set MAIN_REPO)"
fi

echo
if [ "$failures" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "${failures} CHECK(S) FAILED"
fi
exit "$failures"