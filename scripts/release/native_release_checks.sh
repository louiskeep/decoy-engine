#!/usr/bin/env bash
# Shell logic of the native-companion release workflow, factored out so it can be
# dry-run locally (tests/sentry/test_native_release_workflow.py, throwaway key) while
# GitHub Actions is off for this repo. The workflow calls these subcommands verbatim.
#
# Signatures: raw detached Ed25519 over the exact file bytes, base64 in <file>.sig --
# the same bytes `decoy.distribution.signing.verify_detached` (decoy CLI, `decoy
# upgrade`) and `openssl pkeyutl -verify -pubin -rawin` (install.sh) check, and the
# same format `packaging_tools/sign_release.py` writes. Only openssl is used for the
# crypto; nothing here is hand-rolled.
#
# Subcommands (every one exits non-zero on any problem and says why on stderr):
#   sign               --privkey PEM --dir DIR
#   assert-key-matches --pubkey PEM --privkey PEM
#   verify-signatures  --pubkey PEM --dir DIR
#   check-version      --expected VER --native-dir DIR [--dir DIR]
#   check-wheel-set    [--expected VER] --dir DIR
#
# Path options may be relative to the caller's cwd (the workflow passes repo-root
# relative paths); parse_opts makes them absolute before any subcommand cd's.
#   assert-on-main     --sha SHA --main-ref REF
set -euo pipefail

PLACEHOLDER='__DECOY_RELEASE_PUBLIC_KEY__'
PYTHONS=(cp310 cp311 cp312)
# Labels of the four approved companion targets (PLATFORM_RES holds the matching
# rules). macOS carries the deployment target in its tag (macosx_<major>_<minor>_arm64).
PLATFORMS=('manylinux_2_28_x86_64' 'manylinux_2_28_aarch64' 'win_amd64' 'macosx_*_arm64')

die() { echo "native-release: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing tool: $1"; }

refuse_placeholder() {  # $1 public key PEM path
  [ -f "$1" ] || die "public key file not found"
  if grep -q "$PLACEHOLDER" "$1"; then
    die "public key is still the placeholder; fill .github/release-keys/decoy-release-key.pub.pem with the real release public key before releasing"
  fi
}

# --- argument parsing: --name value pairs into $opt_<name> ----------------------
opt_privkey= opt_pubkey= opt_dir= opt_dir_shown= opt_expected= opt_native_dir= opt_sha= opt_main_ref=
# Every option that names a file or directory. sign and verify-signatures cd into
# --dir, so a relative path left as given would resolve against the wrong cwd.
PATH_OPTS=(opt_privkey opt_pubkey opt_dir opt_native_dir)
absolute() {  # $1 path; no symlink resolution, no existence requirement
  case $1 in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s/%s\n' "$PWD" "$1" ;;
  esac
}
parse_opts() {
  while [ $# -gt 0 ]; do
    case $1 in
      --privkey) opt_privkey=$2 ;;
      --pubkey) opt_pubkey=$2 ;;
      --dir) opt_dir=$2 ;;
      --expected) opt_expected=$2 ;;
      --native-dir) opt_native_dir=$2 ;;
      --sha) opt_sha=$2 ;;
      --main-ref) opt_main_ref=$2 ;;
      *) die "unknown option: $1" ;;
    esac
    [ $# -ge 2 ] || die "option $1 needs a value"
    shift 2
  done
  # Messages name --dir as the caller gave it, so the runner's cwd stays out of logs.
  opt_dir_shown=$opt_dir
  local v
  for v in "${PATH_OPTS[@]}"; do
    [ -z "${!v}" ] || printf -v "$v" '%s' "$(absolute "${!v}")"
  done
}
require() { for v in "$@"; do [ -n "${!v}" ] || die "missing required option --${v#opt_}"; done; }

# Files a release set signs: every wheel plus the manifest, nothing else.
signed_files() {  # prints names relative to the cwd (a release dir)
  local f
  for f in *.whl; do [ -e "$f" ] && echo "$f"; done
  [ -e SHA256SUMS ] && echo SHA256SUMS
  return 0
}

# --- sign -----------------------------------------------------------------------
cmd_sign() {
  parse_opts "$@"; require opt_privkey opt_dir; need openssl
  [ -f "$opt_privkey" ] || die "private key file not found"
  cd "$opt_dir"
  local f raw n=0
  raw=$(mktemp)
  trap 'rm -f "$raw"' RETURN
  while IFS= read -r f; do
    openssl pkeyutl -sign -inkey "$opt_privkey" -rawin -in "$f" -out "$raw"
    openssl base64 -A -in "$raw" -out "$f.sig"
    n=$((n + 1))
  done < <(signed_files)
  [ "$n" -gt 0 ] || die "nothing to sign in $opt_dir_shown"
  echo "signed $n file(s)"
}

# --- assert-key-matches ---------------------------------------------------------
# Mirrors packaging_tools/sign_release.py `--assert-published`: compare the RAW
# (DER) public key bytes, so PEM line-wrapping or CRLF differences do not matter.
cmd_assert_key_matches() {
  parse_opts "$@"; require opt_pubkey opt_privkey; need openssl
  refuse_placeholder "$opt_pubkey"
  [ -f "$opt_privkey" ] || die "private key file not found"
  local a b
  a=$(openssl pkey -in "$opt_privkey" -pubout -outform DER 2>/dev/null | openssl dgst -sha256 | awk '{print $NF}') || die "could not derive public key from the private key"
  b=$(openssl pkey -pubin -in "$opt_pubkey" -outform DER 2>/dev/null | openssl dgst -sha256 | awk '{print $NF}') || die "could not read the committed public key"
  [ -n "$a" ] && [ -n "$b" ] || die "could not compare keys"
  [ "$a" = "$b" ] || die "refusing to sign: the signing key's public half does not match the committed release public key"
  echo "signing key matches the committed public key"
}

# --- verify-signatures ----------------------------------------------------------
cmd_verify_signatures() {
  parse_opts "$@"; require opt_pubkey opt_dir; need openssl; need sha256sum
  refuse_placeholder "$opt_pubkey"
  cd "$opt_dir"
  local f raw n=0 bad=0 expected_sigs actual_sigs
  raw=$(mktemp)
  trap 'rm -f "$raw"' RETURN
  while IFS= read -r f; do
    n=$((n + 1))
    if [ ! -f "$f.sig" ]; then echo "native-release: MISSING signature for $f" >&2; bad=1; continue; fi
    if ! openssl base64 -d -A -in "$f.sig" -out "$raw" 2>/dev/null; then
      echo "native-release: UNREADABLE signature for $f" >&2; bad=1; continue
    fi
    if openssl pkeyutl -verify -pubin -inkey "$opt_pubkey" -rawin -in "$f" -sigfile "$raw" >/dev/null 2>&1; then
      echo "verified $f"
    else
      echo "native-release: INVALID signature for $f" >&2; bad=1
    fi
  done < <(signed_files)
  [ "$n" -gt 0 ] || die "no wheels or manifest found in $opt_dir_shown"
  [ -e SHA256SUMS ] || die "SHA256SUMS missing"
  # A signature with no corresponding release file is a stray; refuse rather than
  # publish an asset nobody can account for.
  expected_sigs=$(signed_files | sed 's/$/.sig/' | sort)
  actual_sigs=$(for s in *.sig; do [ -e "$s" ] && echo "$s"; done | sort)
  [ "$expected_sigs" = "$actual_sigs" ] || { echo "native-release: stray or missing .sig files" >&2; bad=1; }
  # The manifest must describe exactly the wheels present, byte for byte.
  sha256sum --strict --check SHA256SUMS >/dev/null || { echo "native-release: SHA256SUMS does not match the wheels" >&2; bad=1; }
  [ "$bad" = 0 ] || die "signature verification FAILED; refusing to publish"
  echo "all $n signed file(s) verified against the committed public key"
}

# --- check-version --------------------------------------------------------------
toml_version() {  # first top-level `version = "..."` in $1
  sed -n 's/^version = "\([^"]*\)".*/\1/p' "$1" | head -n 1
}
# --dir is optional: preflight runs this before any wheel exists (tree agreement
# only); sign-publish runs it again over the real wheels.
cmd_check_version() {
  parse_opts "$@"; require opt_expected opt_native_dir
  # A PEP 440 public version, as the tag carries it after `native-v`: no leading
  # "v", no whitespace, no path characters.
  [[ $opt_expected =~ ^[0-9]+(\.[0-9]+)*((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?$ ]] \
    || die "expected version '$opt_expected' is not a plain PEP 440 version (tag must be native-v<version>)"
  local py cargo f v bad=0 n=0
  py=$(toml_version "$opt_native_dir/pyproject.toml")
  cargo=$(toml_version "$opt_native_dir/Cargo.toml")
  [ "$py" = "$opt_expected" ] || { echo "native-release: pyproject.toml version '$py' != expected '$opt_expected'" >&2; bad=1; }
  [ "$cargo" = "$opt_expected" ] || { echo "native-release: Cargo.toml version '$cargo' != expected '$opt_expected'" >&2; bad=1; }
  if [ -n "$opt_dir" ]; then
    for f in "$opt_dir"/*.whl; do
      [ -e "$f" ] || continue
      n=$((n + 1))
      f=$(basename "$f")
      if ! wheel_fields "$f"; then
        echo "native-release: wheel $f is not a 5-field PEP 427 name (name-version-python-abi-platform; no build tag)" >&2; bad=1
      elif [ "$w_ver" != "$opt_expected" ]; then
        echo "native-release: wheel $f carries version '$w_ver' != expected '$opt_expected'" >&2; bad=1
      fi
    done
    [ "$n" -gt 0 ] || die "no wheels in $opt_dir_shown"
  fi
  [ "$bad" = 0 ] || die "version mismatch; refusing to publish"
  echo "version $opt_expected agrees across tag, pyproject.toml, Cargo.toml and $n wheel(s)"
}

# --- check-wheel-set ------------------------------------------------------------
# Splits a wheel file name into its PEP 427 fields. Exactly five: a build tag (or any
# extra dash) makes it a different name, never a looser match of an approved one.
w_name= w_ver= w_py= w_abi= w_plat=
wheel_fields() {  # $1 file name; sets w_*; non-zero unless exactly 5 non-empty fields
  local stem=${1%.whl} extra
  [ "$stem" != "$1" ] || return 1
  IFS=- read -r w_name w_ver w_py w_abi w_plat extra <<<"$stem"
  [ -z "$extra" ] && [ -n "$w_plat" ] && [ -n "$w_name" ] && [ -n "$w_ver" ] && [ -n "$w_py" ] && [ -n "$w_abi" ]
}
# Anchored platform-tag regexes, one per PLATFORMS label (same order).
PLATFORM_RES=('^manylinux_2_28_x86_64$' '^manylinux_2_28_aarch64$' '^win_amd64$' '^macosx_[0-9]+_[0-9]+_arm64$')
# File names listed in a sha256sum manifest, text (`<hash>  <name>`) or binary
# (`<hash> *<name>`) mode -- the names `sha256sum --check` reads. Fails on any other line.
manifest_names() {  # $1 manifest
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    [[ $line =~ ^[0-9a-f]{64}\ [\ *](.+)$ ]] || return 1
    printf '%s\n' "${BASH_REMATCH[1]}"
  done <"$1"
}
# Exactly one wheel per (python, platform) cell of the approved 4 x 3 matrix, each
# named decoy_engine_native-<version>-<py>-<py>-<platform>.whl, every wheel accounted
# for by a cell, and the manifest listing exactly those wheels. <version> is
# --expected (the workflow always passes it); without it, every wheel must carry the
# same version, so a set can never mix releases.
cmd_check_wheel_set() {
  parse_opts "$@"; require opt_dir
  cd "$opt_dir"
  local py i f count want=$opt_expected bad=0 total=0 matched=0 expected=$(( ${#PYTHONS[@]} * ${#PLATFORMS[@]} ))
  for f in *.whl; do
    [ -e "$f" ] || continue
    total=$((total + 1))
    if [ -z "$want" ] && wheel_fields "$f"; then want=$w_ver; fi
  done
  for py in "${PYTHONS[@]}"; do
    for i in "${!PLATFORMS[@]}"; do
      count=0
      for f in *.whl; do
        [ -e "$f" ] && wheel_fields "$f" || continue
        [ "$w_name" = decoy_engine_native ] && [ "$w_ver" = "$want" ] \
          && [ "$w_py" = "$py" ] && [ "$w_abi" = "$py" ] && [[ $w_plat =~ ${PLATFORM_RES[$i]} ]] \
          && count=$((count + 1))
      done
      if [ "$count" != 1 ]; then
        echo "native-release: expected exactly 1 wheel for $py/${PLATFORMS[$i]}, found $count" >&2; bad=1
      fi
      matched=$((matched + count))
    done
  done
  [ "$total" = "$matched" ] || { echo "native-release: $((total - matched)) wheel(s) match no approved target" >&2; bad=1; }
  [ "$total" = "$expected" ] || { echo "native-release: expected $expected wheels, found $total" >&2; bad=1; }
  if [ -e SHA256SUMS ]; then
    local listed present
    if listed=$(manifest_names SHA256SUMS); then
      listed=$(sort <<<"$listed")
      present=$(for f in *.whl; do [ -e "$f" ] && echo "$f"; done | sort)
      [ "$listed" = "$present" ] || { echo "native-release: SHA256SUMS does not list exactly the wheels present" >&2; bad=1; }
    else
      echo "native-release: SHA256SUMS has a line that is not '<sha256> <space|*><name>'" >&2; bad=1
    fi
  else
    echo "native-release: SHA256SUMS missing" >&2; bad=1
  fi
  [ "$bad" = 0 ] || die "wheel set is not the approved $expected-target matrix; refusing to publish"
  echo "wheel set OK: $total wheels, one per approved target"
}

# --- assert-on-main -------------------------------------------------------------
# A tag (or a dispatch) on a commit that main has not merged must not mint a
# signed release: the signing key only ever signs reviewed main history.
cmd_assert_on_main() {
  parse_opts "$@"; require opt_sha opt_main_ref; need git
  git rev-parse -q --verify "$opt_main_ref^{commit}" >/dev/null || die "cannot resolve $opt_main_ref"
  if git merge-base --is-ancestor "$opt_sha" "$opt_main_ref"; then
    echo "$opt_sha is an ancestor of $opt_main_ref"
  else
    die "commit $opt_sha is not an ancestor of $opt_main_ref; refusing to release unmerged history"
  fi
}

cmd=${1:-}; [ -n "$cmd" ] || die "usage: $0 <sign|assert-key-matches|verify-signatures|check-version|check-wheel-set|assert-on-main> [options]"
shift
case $cmd in
  sign) cmd_sign "$@" ;;
  assert-key-matches) cmd_assert_key_matches "$@" ;;
  verify-signatures) cmd_verify_signatures "$@" ;;
  check-version) cmd_check_version "$@" ;;
  check-wheel-set) cmd_check_wheel_set "$@" ;;
  assert-on-main) cmd_assert_on_main "$@" ;;
  *) die "unknown subcommand: $cmd" ;;
esac
