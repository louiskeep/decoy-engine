#!/usr/bin/env bash
# Cut a decoy-engine release tag from main. Dry run by default; APPLY=1 creates and pushes the tag.
#
# Usage: scripts/release/cut-release.sh EXPECTED_SHA        (dry run: checks only, tags nothing)
#        APPLY=1 scripts/release/cut-release.sh EXPECTED_SHA
#
# EXPECTED_SHA is the full 40-character main commit the release is reviewed at. The tag name is
# v<[project].version from pyproject.toml>, the one version source. In order, the script refuses:
#   - a remote ($RELEASE_REMOTE, default origin) with any fetch or push URL (every `url` and
#     `pushurl`, after insteadOf/pushInsteadOf) that does not match $EXPECTED_REMOTE_RE
#     (default: the GitHub decoy-engine repository);
#   - any checkout but the branch main (another branch, a detached HEAD, a rebase in progress);
#   - a working tree with any tracked change, staged change or untracked file, or an index entry
#     marked assume-unchanged or skip-worktree (git status cannot see those);
#   - HEAD other than EXPECTED_SHA, or a main that differs from the remote main just fetched;
#   - a tag v<version> that already exists locally or on the remote, or a version that is not
#     newer than every existing v* tag;
#   - a CHANGELOG.md whose first section after [Unreleased] is not "## [<version>] - YYYY-MM-DD";
#   - a failure of release_checks.sh (lock environment, version agreement, reason release pin,
#     DP certified profile, SBOM).
# After the checks it re-verifies the tree, HEAD, both tag lookups and the remote URLs (the
# checks run code from the tree), then tags EXPECTED_SHA itself, not whatever HEAD is by then, and
# pushes only that tag ref. Nothing else is published: no GitHub release, no PyPI upload, no
# native build.
set -euo pipefail
unset CDPATH

die() { echo "cut-release: $*" >&2; exit 1; }

expected=${1:-}
[ $# -eq 1 ] || die "usage: [APPLY=1] cut-release.sh EXPECTED_SHA"
[[ $expected =~ ^[0-9a-f]{40}$ ]] || die "EXPECTED_SHA must be a full 40-character lowercase sha"
apply=${APPLY:-0}
[ "$apply" = 0 ] || [ "$apply" = 1 ] || die "APPLY must be 0 or 1"

here="$(cd -P -- "$(dirname -- "$0")" >/dev/null && pwd)"
root="$(git -C "$here" rev-parse --show-toplevel)" || die "not inside a git checkout"
cd "$root"

remote=${RELEASE_REMOTE:-origin}
remote_re=${EXPECTED_REMOTE_RE:-'^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)louiskeep/decoy-engine(\.git)?$'}
# Every URL fetch reads and every URL push writes (several `url`/`pushurl` entries, and
# insteadOf/pushInsteadOf rewrites, as git itself resolves them) must be the engine repository.
remote_destinations_ok() {
  local urls url
  urls="$(git remote get-url --all "$remote" 2>/dev/null)" || die "no remote named $remote"
  while IFS= read -r url; do
    [[ $url =~ $remote_re ]] || die "remote $remote ($url) does not match $remote_re"
  done <<<"$urls"
  urls="$(git remote get-url --push --all "$remote" 2>/dev/null)" || die "no push URL for $remote"
  [ -n "$urls" ] || die "no push URL for $remote"
  while IFS= read -r url; do
    [[ $url =~ $remote_re ]] || die "remote $remote push URL ($url) does not match $remote_re"
  done <<<"$urls"
}
remote_destinations_ok

version="$(sed -n '/^\[project\]$/,/^\[/{s/^version = "\([^"]*\)"$/\1/p;}' pyproject.toml)"
[ -n "$version" ] && [ "$(printf '%s\n' "$version" | wc -l)" -eq 1 ] \
  || die "could not read one static [project].version from pyproject.toml"
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?$ ]] || die "version $version is not a plain release version"
tag="v$version"

clean_main_at_expected() {
  local branch
  branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null)" || branch=""
  [ "$branch" = main ] || die "not on main (HEAD is ${branch:-detached})"
  [ -z "$(git status --porcelain=v1 --untracked-files=all)" ] || die "working tree is not clean"
  # assume-unchanged (lowercase tag) and skip-worktree (S) entries are invisible to git status.
  [ -z "$(git ls-files -v | grep -E '^([a-z]|S) ' || true)" ] \
    || die "the index hides files from git status (assume-unchanged or skip-worktree)"
  [ "$(git rev-parse HEAD)" = "$expected" ] || die "HEAD is $(git rev-parse HEAD), expected $expected"
}

tag_absent() {
  git rev-parse --quiet --verify "refs/tags/$tag" >/dev/null && die "tag $tag already exists locally"
  local remote_tags
  remote_tags="$(git ls-remote --tags "$remote" "refs/tags/$tag")" || die "cannot list tags on $remote"
  [ -z "$remote_tags" ] || die "tag $tag already exists on $remote"
}

clean_main_at_expected
git fetch --quiet --no-tags "$remote" "+refs/heads/main:refs/remotes/$remote/main" \
  || die "cannot fetch main from $remote"
[ "$(git rev-parse "refs/remotes/$remote/main")" = "$expected" ] \
  || die "$remote/main is $(git rev-parse "refs/remotes/$remote/main"), expected $expected"
tag_absent

# Newer than every existing release tag, local and remote (a stale or reused version is refused).
latest="$( { git tag -l 'v*'; git ls-remote --tags --refs "$remote" | sed 's#.*refs/tags/##'; } \
  | sed -n 's/^v\([0-9][0-9.]*\(\(a\|b\|rc\)[0-9]*\)\?\)$/\1/p' | sort -u)"
if [ -n "$latest" ]; then
  newest="$(printf '%s\n%s\n' "$latest" "$version" | sort -V | tail -n1)"
  top="$(printf '%s\n' "$latest" | sort -V | tail -n1)"
  { [ "$newest" = "$version" ] && [ "$top" != "$version" ]; } \
    || die "version $version is not newer than the latest tag v$top"
fi

first_section="$(grep -m2 '^## \[' CHANGELOG.md | sed -n 2p)"
[ "$(grep -m1 '^## \[' CHANGELOG.md)" = "## [Unreleased]" ] \
  && [[ $first_section =~ ^##\ \[${version//./\\.}\]\ -\ [0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] \
  || die "CHANGELOG.md has no dated section for $version right after [Unreleased] (found: ${first_section:-none})"

echo "cut-release: $tag at $expected, preflight passed; running release checks"
"$here/release_checks.sh" "$version" || die "release checks failed"

clean_main_at_expected
tag_absent
remote_destinations_ok

if [ "$apply" != 1 ]; then
  echo "DRY RUN: all checks passed; would create annotated tag $tag at $expected and push it to $remote"
  echo "Re-run with APPLY=1 to tag."
  exit 0
fi

git tag -a "$tag" -m "decoy-engine $version" "$expected"
git push "$remote" "refs/tags/$tag:refs/tags/$tag"
pushed="$(git ls-remote "$remote" "refs/tags/$tag^{}" | cut -f1)"
[ "$pushed" = "$expected" ] || die "remote tag $tag resolves to ${pushed:-nothing}, expected $expected"
echo "cut-release: pushed annotated tag $tag at $expected"
