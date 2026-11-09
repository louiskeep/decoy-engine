#!/usr/bin/env bash
# Run the local gates in CI order and print an EVIDENCE block to paste.
# Usage: scripts/dev/gates.sh [--keep-going] [--baseline] [--baseline-file FILE]
#        [--write-baseline FILE [--order-from FILE]] [--full] [--steps LIST] [--shard I/N]
#        [test paths...]
#   --keep-going  run every step even after a failure (default: stop at the first)
#   --baseline    rerun every step on origin/main in a temporary detached worktree, with the
#                 SAME venv, and diff the failures of every step (test ids; mypy/ruff error
#                 lines). Fails only on branch-only failures.
#   --baseline-file FILE
#                 with --baseline (implied): do NOT rerun origin/main. Load the stored
#                 failing-id baseline FILE, run only the branch pass, then report new
#                 failing ids (exit 1), baseline ids that now pass, and the collected
#                 count delta. Exit 2 before running when FILE is for another repo, was
#                 written in a different run mode (workers, dist, platform, order), or is
#                 behind origin/main in anything but Markdown that no test reads. Fails
#                 unless a whole-suite tests step finished with collected > 0, the branch
#                 skips no more tests than the baseline did, and it collects no fewer
#                 tests than the baseline beyond the test ids in test files the branch
#                 deleted. A missing totals line, or one that disagrees with the JUnit
#                 failure count, exits 3.
#   --write-baseline FILE
#                 after a full-suite run (--full or `tests`, steps include tests, all
#                 steps ran, pytest exit 0 or 1, JUnit with collected > 0) write {repo,
#                 main_sha, head_sha, collected, skipped, failing, workers, dist, platform,
#                 order, order_seconds, file_seconds} to FILE; refused for a selection, and
#                 unless HEAD equals origin/main with a clean tree. Needs origin/main. The
#                 exit code is still 1 after a successful write when main has known failures.
#   --order-from FILE
#                 with --write-baseline: start the longest test files first, ordered by the
#                 file_seconds of the baseline FILE; the order map is stored as order_seconds.
#                 Without it the run keeps collection order. A compare (--baseline-file) orders
#                 by the order_seconds of the baseline it compares with, so its mode matches.
#   --steps LIST  comma list from: env,ruff,format,mypy,sentry,licenses,tests (default: all)
#   --shard I/N   run only shard I of N of the tests step (1 <= I <= N). The selected test
#                 paths are expanded to files, sorted, and dealt round-robin, so each shard
#                 takes every Nth file. Every shard runs the same --steps, so give shards after
#                 the first --steps tests; they do the lint, type and sentry work once. A shard
#                 with no files skips the tests step. Not combinable with --baseline-file or
#                 --write-baseline (a stored baseline is a whole-suite result). --baseline
#                 still works: main reruns the same shard.
#   --full        run the whole `tests` directory (same as passing `tests`)
# GATES_WORKERS=N|auto (default auto) sets the pytest-xdist worker count of the tests step.
#   N > 1 runs two passes that partition the selection: a parallel pass (-n N --dist loadfile,
#   tests marked `serial` deselected), then a plain serial pass of the `serial` tests, which
#   assert on wall-clock, memory or CPU ratios and need the host to themselves. Both JUnit files
#   count as one run. N = 1, or a venv without pytest-xdist, is one plain pytest run. auto is
#   min(cpus, 4), at most 2 on a host under 14 GB (1 when under 4 GB is free). The workers: line
#   of the EVIDENCE header names the choice. A stored baseline records it and refuses a run with
#   another count (exit 2); pin GATES_WORKERS for any baseline write or compare.
#   test paths    default: tests touching files changed against origin/main. That is a
#                 SELECTION, not the full suite; the header says so. Use --full (or `tests`).
set -uo pipefail

# Repo config. Mirrors .github/workflows/ci.yml (ruff, mypy, regression-gate jobs).
REPO_NAME=decoy-engine
RUFF_PATHS=(src tests testflight scripts)
MYPY_ARGS=(src/decoy_engine testflight)
SENTRY_PATHS=(tests/sentry tests/secret_providers/test_leak_sentry.py)
LICENSE_TESTS=(tests/sentry/test_license_policy.py tests/sentry/test_sdv_import_sentry.py)
PYTEST_ARGS=(-m 'not benchmark and not packaging and not codspeed')
PYTEST_MARK=${PYTEST_ARGS[1]}  # the same expression; the parallel and serial passes add to it
# Everything is code unless it matches NON_CODE_GLOBS: a diff with any code path must select at
# least one test (see code_changed). List only pure-documentation or repo-metadata paths here;
# `*` crosses `/`, so 'docs/*' also covers docs/records.
NON_CODE_GLOBS=('docs/*' 'docs/records/*' '*.md' 'LICENSE*' 'NOTICE*' 'CHANGELOG*'
  '.gitignore' '.gitattributes' '.aiignore' '.claude/*' '.tours/*')
SRC_MATCH='src/*.py'   # changed files that map to a dotted module
SRC_STRIP='src/'       # prefix removed before turning a path into a module
PYPATH_DIRS=(src scripts)
IMPORT_PKG=decoy_engine
# Directories a test reads by walking them: a stored baseline that is behind main by a change
# under one of these is stale even when the change is Markdown.
DOC_WALK_ROOTS=(docs/plans/h1-wave-b/manifests)
# Several sentries walk all of docs/ (and root *.md) under one id, so a Markdown fix on main would
# hide a re-break on the branch. Rather than list each walker, any stored failing id under these
# prefixes (every sentry) makes any main move stale.
DOC_WALK_GUARDS=(tests/sentry/ tests/secret_providers/test_leak_sentry.py)

ROOT=$(git rev-parse --show-toplevel) || exit 2
VENV=${DECOY_VENV:-${UV_PROJECT_ENVIRONMENT:-$ROOT/.venv}}
BIN=$VENV/bin
KEEP=0 BASELINE=0 FULL=0 BASEFILE='' WRITEBASE='' ORDERFROM='' SHARD='' STEPS=env,ruff,format,mypy,sentry,licenses,tests PATHS=()
while [ $# -gt 0 ]; do
  case $1 in
    --keep-going) KEEP=1 ;;
    --baseline) BASELINE=1 ;;
    --full) FULL=1 ;;
    --baseline-file) BASEFILE=$2; BASELINE=1; shift ;;
    --write-baseline) WRITEBASE=$2; shift ;;
    --order-from) ORDERFROM=$2; shift ;;
    --steps) STEPS=$2; shift ;;
    --shard) SHARD=$2; shift ;;
    -h|--help) awk 'NR > 1 { if (!/^#/) exit; print }' "$0"; exit 0 ;;
    -*) echo "gates: unknown flag $1" >&2; exit 2 ;;
    *) PATHS+=("$1") ;;
  esac
  shift
done
# A baseline verdict compares every step, so a step skipped after an early failure can never
# read as shared with main.
if [ $BASELINE = 1 ]; then KEEP=1; fi
[ -x "$BIN/python" ] || { echo "gates: no venv at $VENV; run scripts/dev/bootstrap.sh" >&2; exit 2; }
if [ -n "$ORDERFROM" ] && [ -z "$WRITEBASE" ]; then
  echo "gates: --order-from only applies to --write-baseline (a compare orders by the baseline it reads)" >&2; exit 2
fi
# Worker count for the tests step, recorded in a stored baseline's run mode. The reporter owns the
# rule (_gate_workers.py); a venv without pytest-xdist resolves to 1 there and says why.
# Only stdout is captured: the reporter's own diagnostics reach the terminal as written. Exit 64
# is its usage error (GATES_WORKERS is not a number or auto); any other failure is a crash.
WORKERS_RC=0
WORKERS_LINE=$("$BIN/python" "$ROOT/scripts/dev/_gates_report.py" --workers) || WORKERS_RC=$?
if [ $WORKERS_RC != 0 ]; then
  if [ $WORKERS_RC = 64 ]; then exit 2; fi
  echo "gates: FAIL: no complete EVIDENCE block (the reporter failed before the run)" >&2; exit 3
fi
WORKERS=${WORKERS_LINE%% *}
WORKERS_WHY=${WORKERS_LINE#* }
OUT=$(mktemp -d "${TMPDIR:-/tmp}/gates.XXXXXX")
[ -n "$OUT" ] && [ -d "$OUT" ] || { echo "gates: FAIL: cannot create a temp dir" >&2; exit 3; }
WT=$OUT/baseline-tree
MBT=$OUT/merge-base-tree
# Each pytest run gets its own basetemp here, removed on exit: pytest's default
# /tmp/pytest-of-<user> keeps the last three runs, about 14 GB each for the engine suite.
# Kept directly under /tmp, not under $OUT: TMPDIR can be long, and tests that bind
# AF_UNIX sockets under tmp_path fail once the path passes the 104-108 byte limit.
# /tmp on purpose, ignoring TMPDIR: keeps AF_UNIX socket paths under the length limit.
TMP_ROOT=$(mktemp -d /tmp/gt.XXXXXX)
[ -n "$TMP_ROOT" ] && [ -d "$TMP_ROOT" ] || { echo "gates: FAIL: cannot create a basetemp dir" >&2; exit 3; }
mkdir -p "$TMP_ROOT/root"
# Pytest runs that tests start themselves have no --basetemp; this moves their default root too.
export PYTEST_DEBUG_TEMPROOT=$TMP_ROOT/root
# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() {
  local d
  for d in "$WT" "$MBT"; do [ -d "$d" ] && git -C "$ROOT" worktree remove --force "$d" 2>/dev/null; done
  rm -rf -- "${TMP_ROOT:?}"
  return 0
}
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
  for g in ${NON_CODE_GLOBS[@]+"${NON_CODE_GLOBS[@]}"}; do
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
if [ $FULL = 1 ] && [ ${#PATHS[@]} -eq 0 ]; then
  PATHS=(tests)
  SELECTION="full suite (tests)"
elif [ ${#PATHS[@]} -eq 0 ]; then
  if ! git -C "$ROOT" rev-parse -q --verify origin/main >/dev/null; then
    echo "gates: origin/main missing; run: git fetch origin main" >&2; exit 2
  fi
  # No read-into-array builtin: the Mac runner has bash 3.2. A failing selection must fail the gate, not skip tests.
  if ! SELECTED=$(changed_tests); then
    echo "gates: could not select changed tests (merge-base vs origin/main failed)" >&2; exit 2
  fi
  PATHS=()
  while IFS= read -r p; do [ -n "$p" ] && PATHS+=("$p"); done <<EOT
$SELECTED
EOT
  SELECTION="SELECTION ONLY (changed vs origin/main, ${#PATHS[@]} file(s)); this is NOT the full suite, use --full"
  if [ ${#PATHS[@]} -eq 0 ]; then
    code_changed; crc=$?
    [ $crc = 2 ] && { echo "gates: could not diff against the merge base with origin/main" >&2; exit 2; }
    [ $crc = 0 ] && TESTS_REQUIRED=1
  fi
else
  SELECTION="explicit: ${PATHS[*]+${PATHS[*]}}"
  # An explicit path that is missing from the branch is a typo or a deleted test, never a skip.
  if [[ ",$STEPS," == *,tests,* ]]; then
    for p in ${PATHS[@]+"${PATHS[@]}"}; do
      [ -e "$ROOT/${p%%::*}" ] || { echo "gates: FAIL: explicit test path not found: $p" >&2; exit 2; }
    done
  fi
fi

# Shard the test selection. Expanding to files first means one shard never repeats another's
# files; the stored-baseline modes are refused because their result is a whole-suite one.
if [ -n "$SHARD" ]; then
  case $SHARD in
    [1-9]*/[1-9]*) SHARD_I=${SHARD%%/*}; SHARD_N=${SHARD##*/} ;;
    *) echo "gates: --shard wants I/N, got $SHARD" >&2; exit 2 ;;
  esac
  case $SHARD_I$SHARD_N in *[!0-9]*) echo "gates: --shard wants I/N, got $SHARD" >&2; exit 2 ;; esac
  if [ "$SHARD_I" -gt "$SHARD_N" ]; then echo "gates: --shard I must not exceed N ($SHARD)" >&2; exit 2; fi
  if [ -n "$BASEFILE$WRITEBASE" ]; then
    echo "gates: --shard cannot combine with --baseline-file or --write-baseline" >&2; exit 2
  fi
  if [ ${#PATHS[@]} -eq 0 ]; then
    # Nothing selected: shard 1 keeps the normal verdict (not applicable, or a required-test failure).
    if [ "$SHARD_I" -gt 1 ]; then echo "gates: shard $SHARD: no tests selected; shard 1 reports it"; exit 0; fi
  else
    ALL=$(for p in ${PATHS[@]+"${PATHS[@]}"}; do
      if [ -d "$ROOT/$p" ]; then (cd "$ROOT" && git ls-files -- "$p" | grep -E '(^|/)test_[^/]*\.py$' || true); else echo "$p"; fi
    done | sort -u)
    PATHS=()
    n=0
    while IFS= read -r p; do
      [ -n "$p" ] || continue
      [ $((n % SHARD_N)) -eq $((SHARD_I - 1)) ] && PATHS+=("$p")
      n=$((n + 1))
    done <<EOT
$ALL
EOT
    SELECTION="$SELECTION; shard $SHARD (${#PATHS[@]} of $n file(s))"
    if [ ${#PATHS[@]} -eq 0 ]; then
      # An empty slice is not a missing selection: drop the tests step instead of failing it.
      STEPS=$(echo ",$STEPS," | sed 's/,tests,/,/' | sed 's/^,//; s/,$//')
      if [ -z "$STEPS" ]; then echo "gates: shard $SHARD has no test files"; exit 0; fi
    fi
  fi
fi

MAIN_SHA=$(git -C "$ROOT" rev-parse -q --verify origin/main || true)
WHOLE_SUITE=0
[ ${#PATHS[@]} -eq 1 ] && [ "${PATHS[0]}" = tests ] && WHOLE_SUITE=1
if [ -n "$WRITEBASE" ]; then
  # Refuse before spending an hour on a run whose result could not be stored.
  if [ $WHOLE_SUITE = 0 ] || [[ ",$STEPS," != *,tests,* ]]; then
    echo "gates: --write-baseline refused: this run is a selection, not the whole suite (needs --full or the path 'tests', and the tests step)" >&2; exit 2
  fi
  [ -n "$MAIN_SHA" ] || { echo "gates: --write-baseline needs origin/main; run: git fetch origin main" >&2; exit 2; }
  # A baseline describes origin/main, so only a clean checkout of exactly that commit may store one.
  HEAD_SHA=$(git -C "$ROOT" rev-parse HEAD)
  if [ "$HEAD_SHA" != "$MAIN_SHA" ] || [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
    echo "gates: --write-baseline refused: HEAD must equal origin/main ($MAIN_SHA) and the tree must be clean" >&2; exit 2
  fi
fi

# 0 when the stored baseline (main_sha $1) is behind origin/main in something a test could read.
# A main move is harmless only if every changed path is Markdown outside src/ and tests/, outside
# DOC_WALK_ROOTS, and no file under tests/ (helpers included) names it. This is separate from
# NON_CODE_GLOBS, which only decides whether a diff needs tests.
baseline_is_stale() {
  local changed f root
  git -C "$ROOT" cat-file -e "$1^{commit}" 2>/dev/null || { echo "stored main_sha $1 is not in this repository"; return 0; }
  changed=$(git -C "$ROOT" diff --name-only --no-renames "$1" "$MAIN_SHA") || { echo "could not diff $1 against origin/main"; return 0; }
  if [ -n "$changed" ] && "$BIN/python" "$ROOT/scripts/dev/_gates_report.py" --stored-failing-under "$BASEFILE" ${DOC_WALK_GUARDS[@]+"${DOC_WALK_GUARDS[@]}"}; then
    echo "main moved and the stored failing list holds a sentry failure that a Markdown change on main could have hidden"; return 0
  fi
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    case $f in
      src/*|tests/*) echo "main changed $f"; return 0 ;;
      *.md) ;;
      *) echo "main changed $f"; return 0 ;;
    esac
    for root in ${DOC_WALK_ROOTS[@]+"${DOC_WALK_ROOTS[@]}"}; do
      case $f in "$root"/*) echo "main changed $f, which a sentry walks"; return 0 ;; esac
    done
    if grep -rlFq -- "${f##*/}" "$ROOT/tests"; then echo "main changed $f, which a test names"; return 0; fi
  done <<EOT
$changed
EOT
  return 1
}

if [ -n "$BASEFILE" ]; then
  [ -f "$BASEFILE" ] || { echo "gates: baseline file not found: $BASEFILE" >&2; exit 2; }
  stored_sha=$("$BIN/python" "$ROOT/scripts/dev/_gates_report.py" --check-stored "$BASEFILE" "$REPO_NAME" "$WORKERS") || exit 2
  [ -n "$MAIN_SHA" ] || { echo "gates: --baseline-file needs origin/main; run: git fetch origin main" >&2; exit 2; }
  if [ "$MAIN_SHA" != "$stored_sha" ]; then
    if reason=$(baseline_is_stale "$stored_sha"); then
      echo "gates: the stored baseline (main_sha $stored_sha) is stale against origin/main $MAIN_SHA: $reason; rewrite it with --write-baseline" >&2; exit 2
    fi
  fi
  cp "$BASEFILE" "$OUT/stored-baseline.json"
fi

# The collection-order map handed to the suite (tests/conftest.py reads DECOY_TEST_ORDER_FILE): a
# compare orders like the baseline it reads, a write with --order-from by that file's timings.
ORDER_MAP=''
if [ -n "$BASEFILE" ]; then
  ORDER_MAP=$OUT/order-map.json
  "$BIN/python" "$ROOT/scripts/dev/_gates_report.py" --order-map "$ORDER_MAP" "$BASEFILE" order_seconds || exit 2
elif [ -n "$ORDERFROM" ]; then
  [ -f "$ORDERFROM" ] || { echo "gates: --order-from file not found: $ORDERFROM" >&2; exit 2; }
  ORDER_MAP=$OUT/order-map.json
  "$BIN/python" "$ROOT/scripts/dev/_gates_report.py" --order-map "$ORDER_MAP" "$ORDERFROM" file_seconds || exit 2
fi

pypath() { local d out=""; for d in ${PYPATH_DIRS[@]+"${PYPATH_DIRS[@]}"}; do out="$out${out:+:}$1/$d"; done; echo "$out"; }

# Exit code of the two tests passes as one: pytest's 5 (nothing collected) in one pass is not a
# failure when the other pass ran tests; a crash code (not 0, 1 or 5) wins; else 1 beats 0.
combine_rc() {  # $1 parallel-pass rc, $2 serial-pass rc
  local a=$1 b=$2
  if [ "$a" = 5 ] && [ "$b" = 5 ]; then echo 5; return; fi
  [ "$a" = 5 ] && a=0
  [ "$b" = 5 ] && b=0
  case $a in 0|1) ;; *) echo "$a"; return ;; esac
  case $b in 0|1) ;; *) echo "$b"; return ;; esac
  if [ "$a" = 1 ] || [ "$b" = 1 ]; then echo 1; else echo 0; fi
}

run_steps() {  # $1 label, $2 tree
  local label=$1 tree=$2 name rc rc2 stop=0 p cmd cmd2 shown tests=() lic=() pyt w note
  : >"$OUT/$label.steps"
  for name in ${STEPS//,/ }; do
    plain=0 cmd2=() note=
    # tmp_path_retention_policy=failed: a passing test's tmp_path is deleted at its teardown.
    local tmpargs=(-o tmp_path_retention_policy=failed)
    local jbase=(-p no:cacheprovider -q -rfE -o junit_family=xunit1 "--junitxml=$OUT/$label-$name.xml" "--basetemp=$TMP_ROOT/$label-$name" ${tmpargs[@]+"${tmpargs[@]}"})
    local junit=(${jbase[@]+"${jbase[@]}"} ${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"})
    case $name in
      env) cmd=(bash "$ROOT/scripts/dev/bootstrap.sh" --check) ;;
      ruff) cmd=("$BIN/ruff" check --output-format concise ${RUFF_PATHS[@]+"${RUFF_PATHS[@]}"}) ;;
      format) cmd=("$BIN/ruff" format --check ${RUFF_PATHS[@]+"${RUFF_PATHS[@]}"}) ;;
      mypy) cmd=("$BIN/mypy" ${MYPY_ARGS[@]+"${MYPY_ARGS[@]}"}) ;;
      # ratchet_check first (baselines never grow); skipped in trees that predate it (the baseline worktree)
      sentry)
        # strict: the gate venv is the certified profile, so a registry entry that cannot be verified is a failure
        cmd=(env DECOY_GATE_VENV_STRICT=1 bash -c 'if [ -f scripts/dev/ratchet_check.py ]; then "$0" scripts/dev/ratchet_check.py || exit 1; fi; exec "$0" -m pytest "$@"' "$BIN/python" ${junit[@]+"${junit[@]}"} ${SENTRY_PATHS[@]+"${SENTRY_PATHS[@]}"}) ;;
      # strict: a venv that drifted from uv.lock fails instead of skipping the metadata check
      licenses)
        lic=(); for p in ${LICENSE_TESTS[@]+"${LICENSE_TESTS[@]}"}; do [ -e "$tree/$p" ] && lic+=("$p"); done
        cmd=(env DECOY_LICENSE_STRICT=1 "$BIN/python" -m pytest ${junit[@]+"${junit[@]}"} ${lic[@]+"${lic[@]}"}) ;;
      tests)
        tests=(); for p in ${PATHS[@]+"${PATHS[@]}"}; do [ -e "$tree/${p%%::*}" ] && tests+=("$p"); done
        # The order map comes from this script only: an inherited one never reorders a run.
        pyt=(env -u DECOY_TEST_ORDER_FILE ${ORDER_MAP:+"DECOY_TEST_ORDER_FILE=$ORDER_MAP"} "$BIN/python" -m pytest)
        # A stored or compared baseline must come from the whole suite: ignore selections
        # smuggled in through PYTEST_ADDOPTS, PYTEST_PLUGINS or the config's addopts (this
        # script's own -m stays, it is part of the junit array).
        if [ -n "$WRITEBASE$BASEFILE" ]; then
          pyt=(env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS -u DECOY_TEST_ORDER_FILE PY_COLORS=0 NO_COLOR=1 ${ORDER_MAP:+"DECOY_TEST_ORDER_FILE=$ORDER_MAP"} "$BIN/python" -m pytest -o addopts= --color=no)
        fi
        # A tree without the xdist report scrubber (origin/main before it lands) would abort a
        # parallel run on the first lone surrogate in a captured log, so that tree runs serial.
        w=$WORKERS
        note=''
        if [ "$w" -gt 1 ] && [ ! -f "$tree/tests/_xdist_safe_reports.py" ]; then
          w=1; note=" (serial: this tree has no tests/_xdist_safe_reports.py)"
        fi
        if [ "$w" -gt 1 ]; then
          cmd=(${pyt[@]+"${pyt[@]}"} ${jbase[@]+"${jbase[@]}"} -m "($PYTEST_MARK) and not serial" -n "$w" --dist loadfile ${tests[@]+"${tests[@]}"})
          cmd2=(${pyt[@]+"${pyt[@]}"} -p no:cacheprovider -q -rfE -o junit_family=xunit1 "--junitxml=$OUT/$label-tests-serial.xml" "--basetemp=$TMP_ROOT/$label-tests-serial" ${tmpargs[@]+"${tmpargs[@]}"} -m "($PYTEST_MARK) and serial" ${tests[@]+"${tests[@]}"})
        else
          cmd=(${pyt[@]+"${pyt[@]}"} ${junit[@]+"${junit[@]}"} ${tests[@]+"${tests[@]}"})
        fi ;;
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
      (cd "$tree" && PYTHONPATH=$(pypath "$tree") ${cmd[@]+"${cmd[@]}"}) >"$OUT/$label-$name.log" 2>&1
      rc=$?
      if [ ${#cmd2[@]} -gt 0 ]; then
        # Unconditional: the serial pass must run (and be read) even when the parallel pass failed.
        echo "gates: [$label] $name (serial pass) ..." >&2
        (cd "$tree" && PYTHONPATH=$(pypath "$tree") ${cmd2[@]+"${cmd2[@]}"}) >"$OUT/$label-$name-serial.log" 2>&1
        rc2=$?
        if [ $rc2 != 0 ] && [ $rc2 != 5 ]; then tail -n 15 "$OUT/$label-$name-serial.log" | sed "s/^/  $label $name serial| /" >&2; fi
        rc=$(combine_rc "$rc" "$rc2")
      fi
      if [ $rc != 0 ]; then
        tail -n 15 "$OUT/$label-$name.log" | sed "s/^/  $label $name| /" >&2
        [ $KEEP = 1 ] || stop=1
      fi
    fi
    [ $rc = -1 ] || [ $plain = 1 ] && shown=${cmd[*]+${cmd[*]}} || shown=$(printf '%q ' ${cmd[@]+"${cmd[@]}"})
    shown="$shown$note"
    if [ ${#cmd2[@]} -gt 0 ]; then shown="$shown; then $(printf '%q ' ${cmd2[@]+"${cmd2[@]}"})"; fi
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
  echo "workers:    $WORKERS ($WORKERS_WHY)"
  echo "test paths: $SELECTION"
} >"$OUT/meta"

run_steps branch "$ROOT"
if [ -n "$BASEFILE" ]; then
  {
    echo "baseline:   stored file $BASEFILE (main_sha $stored_sha); origin/main NOT rerun"
    echo "run mode:   workers=$WORKERS, order from the baseline's order_seconds (it was written in the same mode)"
    if [ "$MAIN_SHA" != "$stored_sha" ]; then
      echo "NOTE:       origin/main is $MAIN_SHA; main moved only in Markdown no test reads"
    fi
  } >>"$OUT/meta"
elif [ $BASELINE = 1 ]; then
  git -C "$ROOT" worktree add -q --detach "$WT" origin/main || exit 2
  {
    echo "baseline:   origin/main @ $(git -C "$WT" rev-parse HEAD) in a detached worktree, same venv and lock as branch"
    echo "            $IMPORT_PKG imported from: $(cd "$WT" && PYTHONPATH=$(pypath "$WT") "$BIN/python" "$ROOT/scripts/dev/_gates_report.py" --import-origin "$IMPORT_PKG" 2>&1 | tail -n 1)"
  } >>"$OUT/meta"
  run_steps baseline "$WT"
fi
echo "logs:       $OUT" >>"$OUT/meta"

REPORT_ARGS=(--workers "$WORKERS")
[ -n "$ORDERFROM" ] && REPORT_ARGS+=(--order-file "$ORDER_MAP")
[ $WHOLE_SUITE = 1 ] && REPORT_ARGS+=(--whole-suite)
# A whole-suite result is only trustworthy if the tests step ran the whole suite,
# pytest finished (rc 0 or 1), and its JUnit file parses with at least one case.
tests_rc=$(awk -F'\t' '$1=="tests"{print $2}' "$OUT/branch.steps")
if awk -F'\t' '$2 == -2 {hit = 1} END {exit !hit}' "$OUT/branch.steps"; then tests_rc=skipped; fi
SUITE_OK=0
if [ $WHOLE_SUITE = 1 ] && { [ "$tests_rc" = 0 ] || [ "$tests_rc" = 1 ]; } &&
  [ "$("$BIN/python" "$ROOT/scripts/dev/_gates_report.py" --suite-collected "$OUT" 2>/dev/null)" -gt 0 ] 2>/dev/null; then
  SUITE_OK=1
fi
if [ -n "$WRITEBASE" ]; then
  if [ $SUITE_OK = 1 ]; then
    REPORT_ARGS+=(--write-baseline "$WRITEBASE" --repo "$REPO_NAME" --main-sha "$MAIN_SHA" --head-sha "$HEAD_SHA")
  else
    echo "gates: --write-baseline refused: the tests step did not finish the whole suite with a JUnit file and collected > 0 (tests rc ${tests_rc:-none}); use --keep-going" >&2
    WRITE_REFUSED=1
  fi
fi

# Test ids in the test files this branch deleted, counted at the merge base. A rename is not a
# deletion (git -M), so a moved file keeps its ids in the branch's own count.
deleted_test_ids() {
  local base f files=() n
  base=$(git -C "$ROOT" merge-base origin/main HEAD) || return 1
  while IFS= read -r f; do
    case $f in tests/*test_*.py) files+=("$f") ;; esac
  done <<EOT
$(git -C "$ROOT" diff --name-only -M --diff-filter=D "$base")
EOT
  [ ${#files[@]} -gt 0 ] || { echo 0; return 0; }
  git -C "$ROOT" worktree add -q --detach "$MBT" "$base" || return 1
  # A collection error in a deleted file counts no ids, so the shortfall stays and the gate fails.
  n=$(cd "$MBT" && PYTHONPATH=$(pypath "$MBT") env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS "$BIN/python" -m pytest --collect-only -q -p no:cacheprovider -o addopts= --color=no ${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"} ${files[@]+"${files[@]}"} 2>/dev/null | grep -cE '^[^ ]+\.py::') || true
  echo "${n:-0}"
}

# A gate passes only on a complete EVIDENCE block and at least one step that ran green.
"$BIN/python" "$ROOT/scripts/dev/_gates_report.py" "$OUT" ${REPORT_ARGS[@]+"${REPORT_ARGS[@]}"} | tee "$OUT/evidence"
report_rc=${PIPESTATUS[0]}
if [ "$report_rc" = 4 ]; then
  echo "gates: FAIL: the failure count in the pytest log disagrees with the JUnit (logs in $OUT)" >&2; exit 3
fi
if [ "$report_rc" != 0 ] || ! grep -qx '=* EVIDENCE =*' "$OUT/evidence" || ! tail -n 1 "$OUT/evidence" | grep -qx '=\{70\}'; then
  echo "gates: FAIL: no complete EVIDENCE block (reporter exit $report_rc; logs in $OUT)" >&2; exit 3
fi
[ "${WRITE_REFUSED:-0}" = 1 ] && exit 1
if [ -n "$BASEFILE" ]; then
  [ $SUITE_OK = 1 ] || { echo "gates: FAIL: baseline comparison needs a completed whole-suite tests step with a JUnit file and collected > 0 (tests rc ${tests_rc:-none})" >&2; exit 1; }
  # A smaller suite is green only when the branch itself deleted those tests; a collection error
  # in a kept file shrinks the count too and must not read as a smaller, passing suite.
  if [ -s "$OUT/shortfall" ]; then
    short=$(cat "$OUT/shortfall")
    if ! deleted=$(deleted_test_ids); then echo "gates: FAIL: could not count the tests the branch deleted" >&2; exit 1; fi
    if [ "$short" -gt "$deleted" ]; then
      echo "gates: FAIL: the branch collects $short fewer tests than the baseline but deleted only $deleted" >&2; exit 1
    fi
    echo "gates: collected $short fewer tests than the baseline; the branch deleted $deleted" >&2
  fi
  # Known failures are not a failure of the branch: only a new id, a failing
  # non-test step, or a pytest run that did not finish blocks.
  [ -s "$OUT/new-failing" ] && exit 1
  [ -s "$OUT/skip-increase" ] && exit 1
  awk -F'\t' '$1!="tests" && $2!=0 && $2!=-1 {bad=1} $1=="tests" && $2!=0 && $2!=1 && $2!=-1 {bad=1} END{exit !bad}' "$OUT/branch.steps" && exit 1
elif [ $BASELINE = 1 ]; then
  # Same host, same venv: a step that also fails on origin/main is a host or main problem, not this
  # branch's. Fail only on branch-only failures (see _gates_report.branch_only_steps).
  if ! BRANCH_ONLY=$("$BIN/python" "$ROOT/scripts/dev/_gates_report.py" --verdict "$OUT"); then
    echo "gates: FAIL: could not compute the baseline verdict" >&2; exit 3
  fi
  [ -n "$BRANCH_ONLY" ] && exit 1
else
  # One awk reads the whole file: a cut | grep -q pipe under pipefail can SIGPIPE and drop a failure.
  awk -F'\t' '$2 != 0 && $2 != -1 {bad = 1} END {exit !bad}' "$OUT/branch.steps" && exit 1
fi
# A tests step that ended rc 1 only on known baseline failures did run.
ran_max=0; [ -n "$BASEFILE" ] && ran_max=1
awk -F'\t' -v max="$ran_max" '$2 >= 0 && $2 <= max {ran = 1} END {exit !ran}' "$OUT/branch.steps" || { echo "gates: FAIL: no step ran (all skipped or none selected)" >&2; exit 3; }
exit 0
