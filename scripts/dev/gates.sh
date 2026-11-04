#!/usr/bin/env bash
# Run the local gates in CI order and print an EVIDENCE block to paste.
# Usage: scripts/dev/gates.sh [--keep-going] [--baseline] [--steps LIST] [test paths...]
#   --keep-going  run every step even after a failure (default: stop at the first)
#   --baseline    rerun the same steps on origin/main in a temporary detached
#                 worktree, with the SAME venv, and diff the failures of every step
#                 (test ids; mypy/ruff error lines). Fails only on branch-only failures.
#   --steps LIST  comma list from: env,ruff,format,mypy,sentry,licenses,tests (default: all)
#   test paths    default: tests touching files changed against origin/main
set -uo pipefail

# Repo config. Mirrors .github/workflows/ci.yml (ruff, mypy, regression-gate jobs).
REPO_NAME=decoy-engine
RUFF_PATHS=(src tests testflight scripts)
MYPY_ARGS=(src/decoy_engine testflight)
SENTRY_PATHS=(tests/sentry tests/secret_providers/test_leak_sentry.py)
LICENSE_TESTS=(tests/sentry/test_license_policy.py tests/sentry/test_sdv_import_sentry.py)
PYTEST_ARGS=(-m 'not benchmark and not packaging and not codspeed')
# Everything is code unless it matches NON_CODE_GLOBS: a diff with any code path must select at
# least one test (see code_changed). List only pure-documentation or repo-metadata paths here;
# `*` crosses `/`, so 'docs/*' also covers docs/records.
NON_CODE_GLOBS=('docs/*' 'docs/records/*' '*.md' 'LICENSE*' 'NOTICE*' 'CHANGELOG*'
  '.gitignore' '.gitattributes' '.aiignore' '.claude/*' '.tours/*')
SRC_MATCH='src/*.py'   # changed files that map to a dotted module
SRC_STRIP='src/'       # prefix removed before turning a path into a module
PYPATH_DIRS=(src scripts)
IMPORT_PKG=decoy_engine

ROOT=$(git rev-parse --show-toplevel) || exit 2
VENV=${DECOY_VENV:-${UV_PROJECT_ENVIRONMENT:-$ROOT/.venv}}
BIN=$VENV/bin
KEEP=0 BASELINE=0 STEPS=env,ruff,format,mypy,sentry,licenses,tests PATHS=()
while [ $# -gt 0 ]; do
  case $1 in
    --keep-going) KEEP=1 ;;
    --baseline) BASELINE=1 ;;
    --steps) STEPS=$2; shift ;;
    -h|--help) sed -n 2,8p "$0"; exit 0 ;;
    -*) echo "gates: unknown flag $1" >&2; exit 2 ;;
    *) PATHS+=("$1") ;;
  esac
  shift
done
[ -x "$BIN/python" ] || { echo "gates: no venv at $VENV; run scripts/dev/bootstrap.sh" >&2; exit 2; }
OUT=$(mktemp -d "${TMPDIR:-/tmp}/gates.XXXXXX")
WT=$OUT/baseline-tree
# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() { [ -d "$WT" ] && git -C "$ROOT" worktree remove --force "$WT" 2>/dev/null; }
trap cleanup EXIT

changed_tests() {
  local base f mod
  base=$(git -C "$ROOT" merge-base origin/main HEAD) || return 1
  { git -C "$ROOT" diff --name-only --no-renames "$base"; git -C "$ROOT" ls-files --others --exclude-standard; } |
    sort -u | while read -r f; do
      [ -f "$ROOT/$f" ] || continue
      # shellcheck disable=SC2254  # SRC_MATCH is a glob on purpose
      case $f in
        tests/*test_*.py) echo "$f" ;;
        $SRC_MATCH)
          mod=${f#"$SRC_STRIP"}; mod=${mod%.py}; mod=${mod%/__init__}; mod=${mod//\//.}
          (cd "$ROOT" && grep -rlE --include='test_*.py' "${mod//./\\.}([^A-Za-z0-9_]|\$)" tests || true) ;;  # no match is normal, not a failed selection
        *)
          # Other code (scripts, native, fixtures): select tests that name the file. No match is
          # normal here; code_changed then makes the gate fail rather than skip.
          non_code "$f" || (cd "$ROOT" && grep -rlF --include='test_*.py' "${f##*/}" tests || true) ;;
      esac
    done | sort -u
}

# 0 when f matches NON_CODE_GLOBS (pure documentation or repo metadata).
non_code() {
  local g
  for g in "${NON_CODE_GLOBS[@]}"; do
    # shellcheck disable=SC2254  # NON_CODE_GLOBS entries are globs on purpose
    case $1 in $g) return 0 ;; esac
  done
  return 1
}

# 0 when the diff against the merge base touches any path outside NON_CODE_GLOBS (deleted files
# count). Inverted on purpose: a new importable package is code by default, never "not applicable".
code_changed() {
  local base f
  base=$(git -C "$ROOT" merge-base origin/main HEAD) || return 2
  while read -r f; do
    [ -n "$f" ] || continue
    non_code "$f" || return 0
  done <<EOT
$({ git -C "$ROOT" diff --name-only --no-renames "$base"; git -C "$ROOT" ls-files --others --exclude-standard; } | sort -u)
EOT
  return 1
}

TESTS_REQUIRED=0
if [ ${#PATHS[@]} -eq 0 ]; then
  if ! git -C "$ROOT" rev-parse -q --verify origin/main >/dev/null; then
    echo "gates: origin/main missing; run: git fetch origin main" >&2; exit 2
  fi
  # No mapfile: the Mac runner has bash 3.2. A failing selection must fail the gate, not skip tests.
  if ! SELECTED=$(changed_tests); then
    echo "gates: could not select changed tests (merge-base vs origin/main failed)" >&2; exit 2
  fi
  PATHS=()
  while IFS= read -r p; do [ -n "$p" ] && PATHS+=("$p"); done <<EOT
$SELECTED
EOT
  SELECTION="changed vs origin/main: ${#PATHS[@]} test file(s)"
  if [ ${#PATHS[@]} -eq 0 ]; then
    code_changed; crc=$?
    [ $crc = 2 ] && { echo "gates: could not diff against the merge base with origin/main" >&2; exit 2; }
    [ $crc = 0 ] && TESTS_REQUIRED=1
  fi
else
  SELECTION="explicit: ${PATHS[*]}"
  # An explicit path that is missing from the branch is a typo or a deleted test, never a skip.
  if [[ ",$STEPS," == *,tests,* ]]; then
    for p in "${PATHS[@]}"; do
      [ -e "$ROOT/${p%%::*}" ] || { echo "gates: FAIL: explicit test path not found: $p" >&2; exit 2; }
    done
  fi
fi

pypath() { local d out=""; for d in "${PYPATH_DIRS[@]}"; do out="$out${out:+:}$1/$d"; done; echo "$out"; }

run_steps() {  # $1 label, $2 tree
  local label=$1 tree=$2 name rc stop=0 p cmd shown tests=() lic=()
  : >"$OUT/$label.steps"
  for name in ${STEPS//,/ }; do
    plain=0
    local junit=(-p no:cacheprovider -q -rfE "--junitxml=$OUT/$label-$name.xml" "${PYTEST_ARGS[@]}")
    case $name in
      env) cmd=(bash "$ROOT/scripts/dev/bootstrap.sh" --check) ;;
      ruff) cmd=("$BIN/ruff" check --output-format concise "${RUFF_PATHS[@]}") ;;
      format) cmd=("$BIN/ruff" format --check "${RUFF_PATHS[@]}") ;;
      mypy) cmd=("$BIN/mypy" "${MYPY_ARGS[@]}") ;;
      # ratchet_check first (baselines never grow); skipped in trees that predate it (the baseline worktree)
      sentry)
        cmd=(bash -c 'if [ -f scripts/dev/ratchet_check.py ]; then "$0" scripts/dev/ratchet_check.py || exit 1; fi; exec "$0" -m pytest "$@"' "$BIN/python" "${junit[@]}" "${SENTRY_PATHS[@]}") ;;
      # strict: a venv that drifted from uv.lock fails instead of skipping the metadata check
      licenses)
        lic=(); for p in "${LICENSE_TESTS[@]}"; do [ -e "$tree/$p" ] && lic+=("$p"); done
        cmd=(env DECOY_LICENSE_STRICT=1 "$BIN/python" -m pytest "${junit[@]}" "${lic[@]}") ;;
      tests)
        tests=(); for p in "${PATHS[@]}"; do [ -e "$tree/${p%%::*}" ] && tests+=("$p"); done
        cmd=("$BIN/python" -m pytest "${junit[@]}" "${tests[@]}") ;;
      *) echo "gates: unknown step $name" >&2; exit 2 ;;
    esac
    if [ $stop = 1 ]; then rc=-2
    elif [ "$name" = tests ] && [ ${#tests[@]} -eq 0 ] && [ "$label" = branch ] && [ $TESTS_REQUIRED = 1 ]; then
      plain=1; rc=1; cmd=("(FAIL: code changed but no tests were selected; pass test paths or add a test that imports the changed module)")
      echo "gates: [$label] tests FAIL: the diff touches code (any path outside NON_CODE_GLOBS) but selects no tests" >&2
      [ $KEEP = 1 ] || stop=1
    elif [ "$name" = tests ] && [ ${#tests[@]} -eq 0 ] && [ "$label" = branch ] && [ ${#PATHS[@]} -eq 0 ]; then
      rc=-1; cmd=("(not applicable: the diff touches no code paths)")
    elif [ "$name" = tests ] && [ ${#tests[@]} -eq 0 ]; then rc=-1; cmd=("(no test paths present)")
    elif [ "$name" = licenses ] && [ ${#lic[@]} -eq 0 ]; then rc=-1; cmd=("(no license tests in this tree)")
    elif [ "$name" = env ] && [ "$label" = baseline ]; then rc=-1; cmd=("(same venv as branch)")
    else
      echo "gates: [$label] $name ..." >&2
      (cd "$tree" && PYTHONPATH=$(pypath "$tree") "${cmd[@]}") >"$OUT/$label-$name.log" 2>&1
      rc=$?
      if [ $rc != 0 ]; then
        tail -n 15 "$OUT/$label-$name.log" | sed "s/^/  $label $name| /" >&2
        [ $KEEP = 1 ] || stop=1
      fi
    fi
    [ $rc = -1 ] || [ $plain = 1 ] && shown=${cmd[*]} || shown=$(printf '%q ' "${cmd[@]}")
    printf '%s\t%s\t%s\n' "$name" "$rc" "$shown" >>"$OUT/$label.steps"
  done
}

ver() { local v; v=$("$@" 2>/dev/null | head -n 1); echo "${v:-not installed}"; }
{
  echo "repo:       $REPO_NAME @ $(git -C "$ROOT" rev-parse HEAD) ($(git -C "$ROOT" branch --show-current))"
  echo "dirty:      $(git -C "$ROOT" status --porcelain | wc -l) path(s) uncommitted"
  echo "origin/main $(git -C "$ROOT" rev-parse -q --verify origin/main || echo missing)"
  echo "venv:       $VENV"
  echo "python:     $(ver "$BIN/python" --version)"
  echo "ruff:       $(ver "$BIN/ruff" --version)"
  echo "mypy:       $(ver "$BIN/mypy" --version)"
  echo "pytest:     $(ver "$BIN/python" -m pytest --version)"
  echo "steps:      $STEPS (keep-going=$KEEP)"
  echo "test paths: $SELECTION"
} >"$OUT/meta"

run_steps branch "$ROOT"
if [ $BASELINE = 1 ]; then
  git -C "$ROOT" worktree add -q --detach "$WT" origin/main || exit 2
  {
    echo "baseline:   origin/main @ $(git -C "$WT" rev-parse HEAD) in a detached worktree, same venv and lock as branch"
    echo "            $IMPORT_PKG imported from: $(cd "$WT" && PYTHONPATH=$(pypath "$WT") "$BIN/python" -c "import $IMPORT_PKG as m; print(m.__file__)" 2>&1 | tail -n 1)"
  } >>"$OUT/meta"
  run_steps baseline "$WT"
fi
echo "logs:       $OUT" >>"$OUT/meta"

# A gate passes only on a complete EVIDENCE block and at least one step that ran green.
"$BIN/python" "$ROOT/scripts/dev/_gates_report.py" "$OUT" | tee "$OUT/evidence"
report_rc=${PIPESTATUS[0]}
if [ "$report_rc" != 0 ] || ! grep -qx '=* EVIDENCE =*' "$OUT/evidence" || ! tail -n 1 "$OUT/evidence" | grep -qx '=\{70\}'; then
  echo "gates: FAIL: no complete EVIDENCE block (reporter exit $report_rc; logs in $OUT)" >&2; exit 3
fi
if [ $BASELINE = 1 ]; then
  # Same host, same venv: a step that also fails on origin/main is a host or main problem, not this
  # branch's. Fail only on branch-only failures (see _gates_report.branch_only_steps).
  if ! BRANCH_ONLY=$("$BIN/python" "$ROOT/scripts/dev/_gates_report.py" --verdict "$OUT"); then
    echo "gates: FAIL: could not compute the baseline verdict" >&2; exit 3
  fi
  [ -n "$BRANCH_ONLY" ] && exit 1
else
  cut -f2 "$OUT/branch.steps" | grep -qvE '^(0|-1)$' && exit 1
fi
cut -f2 "$OUT/branch.steps" | grep -qx 0 || { echo "gates: FAIL: no step ran (all skipped or none selected)" >&2; exit 3; }
exit 0
