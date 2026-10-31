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
#   check-wheel-set    --dir DIR
#   assert-on-main     --sha SHA --main-ref REF
set -euo pipefail

PLACEHOLDER='__DECOY_RELEASE_PUBLIC_KEY__'
PYTHONS=(cp310 cp311 cp312)
# Platform-tag patterns of the four approved companion targets. macOS carries the
# deployment target in its tag (macosx_<major>_<minor>_arm64), so it is a glob.
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
opt_privkey= opt_pubkey= opt_dir= opt_expected= opt_native_dir= opt_sha= opt_main_ref=
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
  [ "$n" -gt 0 ] || die "nothing to sign in $opt_dir"
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
  [ "$n" -gt 0 ] || die "no wheels or manifest found in $opt_dir"
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
      v=$(basename "$f" | cut -d- -f2)
      [ "$v" = "$opt_expected" ] || { echo "native-release: wheel $(basename "$f") carries version '$v' != expected '$opt_expected'" >&2; bad=1; }
    done
    [ "$n" -gt 0 ] || die "no wheels in $opt_dir"
  fi
  [ "$bad" = 0 ] || die "version mismatch; refusing to publish"
  echo "version $opt_expected agrees across tag, pyproject.toml, Cargo.toml and $n wheel(s)"
}

# --- check-wheel-set ------------------------------------------------------------
# Exactly one wheel per (python, platform) cell of the approved 4 x 3 matrix, every
# wheel accounted for by a cell, and the manifest listing exactly those wheels.
cmd_check_wheel_set() {
  parse_opts "$@"; require opt_dir
  cd "$opt_dir"
  local py plat f count bad=0 total=0 matched=0 expected=$(( ${#PYTHONS[@]} * ${#PLATFORMS[@]} ))
  for f in *.whl; do [ -e "$f" ] && total=$((total + 1)); done
  for py in "${PYTHONS[@]}"; do
    for plat in "${PLATFORMS[@]}"; do
      count=0
      for f in decoy_engine_native-*-"$py"-"$py"-$plat.whl; do [ -e "$f" ] && count=$((count + 1)); done
      if [ "$count" != 1 ]; then
        echo "native-release: expected exactly 1 wheel for $py/$plat, found $count" >&2; bad=1
      fi
      matched=$((matched + count))
    done
  done
  [ "$total" = "$matched" ] || { echo "native-release: $((total - matched)) wheel(s) match no approved target" >&2; bad=1; }
  [ "$total" = "$expected" ] || { echo "native-release: expected $expected wheels, found $total" >&2; bad=1; }
  if [ -e SHA256SUMS ]; then
    local listed present
    listed=$(awk '{print $2}' SHA256SUMS | sort)
    present=$(for f in *.whl; do [ -e "$f" ] && echo "$f"; done | sort)
    [ "$listed" = "$present" ] || { echo "native-release: SHA256SUMS does not list exactly the wheels present" >&2; bad=1; }
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
