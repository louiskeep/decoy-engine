#!/usr/bin/env bash
# Run the local gates in CI order and print an EVIDENCE block to paste.
# Usage: scripts/dev/gates.sh [--keep-going] [--baseline] [--steps LIST] [test paths...]
#   --keep-going  run every step even after a failure (default: stop at the first)
#   --baseline    rerun the same steps on origin/main in a temporary detached
#                 worktree, with the SAME venv, and diff the failing test ids
#   --steps LIST  comma list from: env,ruff,format,mypy,sentry,tests (default: all)
#   test paths    default: tests touching files changed against origin/main
set -uo pipefail

# Repo config. Mirrors .github/workflows/ci.yml (ruff, mypy, regression-gate jobs).
REPO_NAME=decoy-engine
RUFF_PATHS=(src tests testflight scripts)
MYPY_ARGS=(src/decoy_engine testflight)
SENTRY_PATHS=(tests/sentry)
PYTEST_ARGS=(-m 'not benchmark and not packaging and not codspeed')
SRC_MATCH='src/*.py'   # changed files that map to a dotted module
SRC_STRIP='src/'       # prefix removed before turning a path into a module
PYPATH_DIRS=(src scripts)
IMPORT_PKG=decoy_engine

ROOT=$(git rev-parse --show-toplevel) || exit 2
VENV=${DECOY_VENV:-${UV_PROJECT_ENVIRONMENT:-$ROOT/.venv}}
BIN=$VENV/bin
KEEP=0 BASELINE=0 STEPS=env,ruff,format,mypy,sentry,tests PATHS=()
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
cleanup() { [ -d "$WT" ] && git -C "$ROOT" worktree remove --force "$WT" 2>/dev/null; }
trap cleanup EXIT

changed_tests() {
  local base f mod
  base=$(git -C "$ROOT" merge-base origin/main HEAD) || return 1
  { git -C "$ROOT" diff --name-only "$base"; git -C "$ROOT" ls-files --others --exclude-standard; } |
    sort -u | while read -r f; do
      [ -f "$ROOT/$f" ] || continue
      case $f in
        tests/*test_*.py) echo "$f" ;;
        $SRC_MATCH)
          mod=${f#"$SRC_STRIP"}; mod=${mod%.py}; mod=${mod%/__init__}; mod=${mod//\//.}
          (cd "$ROOT" && grep -rlE --include='test_*.py' "${mod//./\\.}([^A-Za-z0-9_]|\$)" tests) ;;
      esac
    done | sort -u
}

if [ ${#PATHS[@]} -eq 0 ]; then
  if ! git -C "$ROOT" rev-parse -q --verify origin/main >/dev/null; then
    echo "gates: origin/main missing; run: git fetch origin main" >&2; exit 2
  fi
  mapfile -t PATHS < <(changed_tests)
  SELECTION="changed vs origin/main: ${#PATHS[@]} test file(s)"
else
  SELECTION="explicit: ${PATHS[*]}"
fi

pypath() { local d out=""; for d in "${PYPATH_DIRS[@]}"; do out="$out${out:+:}$1/$d"; done; echo "$out"; }

run_steps() {  # $1 label, $2 tree
  local label=$1 tree=$2 name rc stop=0 p cmd tests=()
  : >"$OUT/$label.steps"
  for name in ${STEPS//,/ }; do
    local junit=(-p no:cacheprovider -q -rfE "--junitxml=$OUT/$label-$name.xml" "${PYTEST_ARGS[@]}")
    case $name in
      env) cmd=(bash "$ROOT/scripts/dev/bootstrap.sh" --check) ;;
      ruff) cmd=("$BIN/ruff" check "${RUFF_PATHS[@]}") ;;
      format) cmd=("$BIN/ruff" format --check "${RUFF_PATHS[@]}") ;;
      mypy) cmd=("$BIN/mypy" "${MYPY_ARGS[@]}") ;;
      sentry) cmd=("$BIN/python" -m pytest "${junit[@]}" "${SENTRY_PATHS[@]}") ;;
      tests)
        tests=(); for p in "${PATHS[@]}"; do [ -e "$tree/${p%%::*}" ] && tests+=("$p"); done
        cmd=("$BIN/python" -m pytest "${junit[@]}" "${tests[@]}") ;;
      *) echo "gates: unknown step $name" >&2; exit 2 ;;
    esac
    if [ $stop = 1 ]; then rc=-2
    elif [ "$name" = tests ] && [ ${#tests[@]} -eq 0 ]; then rc=-1; cmd=("(no test paths present)")
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
    printf '%s\t%s\t%s\n' "$name" "$rc" "${cmd[*]}" >>"$OUT/$label.steps"
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

"$BIN/python" "$ROOT/scripts/dev/_gates_report.py" "$OUT"
cut -f2 "$OUT/branch.steps" | grep -qvE '^(0|-1)$' && exit 1
exit 0
