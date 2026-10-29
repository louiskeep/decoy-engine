#!/usr/bin/env bash
# F25 Slice 4 A02/A03 real-kernel re-run of the MIGRATED writers (GCP host).
#
# Same privilege split as the Slice-1 driver: the project-quota guarantee only
# holds for an UNPRIVILEGED writer on an ext4 mount with project quota enforced,
# so root provisions (mkfs/mount/quotaon/setquota/chattr +P) and the migrated
# writers run as the unprivileged user. Running the writers as root would give a
# false pass (Slice-0 report condition 1).
#
# Unlike the Slice-1 driver (a pure-stdlib scratch bundle), the migrated writers
# need the full engine plus pyarrow/duckdb/pandas, so the bundle carries a venv
# built with those deps and the suite runs with PYTHONPATH=<bundle>/src.
#
# Usage (on the GCP host, as a user with sudo):
#   scripts/f25/run_slice4_fs_tests.sh <bundle_dir> [wuser]
#
# <bundle_dir> holds the engine tree with a prepared .venv (pytest + base deps):
#   decoy-engine-f25/{src,tests,.venv}. The script provisions a loop-mounted ext4
#   image with a small project quota, hands the arena to the unprivileged user,
#   and runs tests/scratch/test_f25_slice4_migrated_fs.py as that user.
set -u
BUNDLE="${1:?usage: run_slice4_fs_tests.sh <bundle_dir> [wuser]}"
WUSER="${2:-decoyf25}"
PROJID=921                           # data project (small caps)
CPROJID=922                          # control project (generous, separate domain)
QUOTA_BYTES=$((8 * 1024 * 1024))     # 8 MiB hard byte cap
QUOTA_KB=$((QUOTA_BYTES / 1024))
QUOTA_INODES=64

if [ "$(id -u)" -ne 0 ]; then
  echo "provisioning needs root; re-run under sudo" >&2
  exit 2
fi
if [ ! -x "$BUNDLE/.venv/bin/python" ]; then
  echo "bundle $BUNDLE has no prepared .venv (expected $BUNDLE/.venv/bin/python)" >&2
  exit 2
fi

# Root-owned private workdir created with mktemp (0700) to avoid a predictable
# /tmp symlink-race; the unprivileged writer gets traverse (0711) only.
WORKDIR="$(mktemp -d /var/tmp/f25s4.XXXXXX)"
if [ "$(stat -c '%u %a' "$WORKDIR")" != "0 700" ]; then
  echo "workdir $WORKDIR is not root-owned 0700; refusing" >&2
  exit 2
fi
chmod 0711 "$WORKDIR"
IMG="$WORKDIR/slice4.img"
MNT="$WORKDIR/mnt"
ARENA="$MNT/slice4-arena"
CONTROL="$MNT/slice4-control"
LOOP=""
cleanup() {
  umount "$MNT" 2>/dev/null || true
  [ -n "$LOOP" ] && losetup -d "$LOOP" 2>/dev/null || true
  rm -rf "$WORKDIR" 2>/dev/null || true
}
trap cleanup EXIT

echo "== provisioning ext4 + project quota (root) =="
mkdir -p "$MNT"
install -m 600 /dev/null "$IMG"
truncate -s 256M "$IMG"
mkfs.ext4 -F -b 4096 -O encrypt,project,quota "$IMG" >/dev/null 2>&1
LOOP="$(losetup --find --show "$IMG")"
mount -o prjquota,nosuid,nodev,noexec "$LOOP" "$MNT"
quotaon -Pp "$MNT" >/dev/null 2>&1 || true
mkdir -p "$ARENA"
chattr +P -p "$PROJID" "$ARENA"
chown "$WUSER:$WUSER" "$ARENA"
setquota -P "$PROJID" 0 "$QUOTA_KB" 0 "$QUOTA_INODES" "$MNT"
# Control arena: its OWN project with generous caps, so a data breach can always
# be recorded (plan 1.5 control-arena sizing).
mkdir -p "$CONTROL"
chattr +P -p "$CPROJID" "$CONTROL"
chown "$WUSER:$WUSER" "$CONTROL"
setquota -P "$CPROJID" 0 $((64 * 1024)) 0 10000 "$MNT"
echo "data arena: $ARENA  byte cap: ${QUOTA_KB}KB  inode cap: $QUOTA_INODES"
echo "control arena: $CONTROL  (project $CPROJID, separate domain)"
repquota -P "$MNT" | grep -E "#$PROJID|#$CPROJID|$PROJID|$CPROJID" || true

echo "== running A02/A03 migrated-writer suite (unprivileged) =="
sudo -u "$WUSER" bash -s <<EOF
set -e
cd "$BUNDLE"
echo "== running as \$(whoami) (must NOT be root) =="
DECOY_F25_QUOTA_ROOT="$ARENA" \
DECOY_F25_CONTROL_ROOT="$CONTROL" \
DECOY_F25_QUOTA_BYTES="$QUOTA_BYTES" \
DECOY_F25_QUOTA_INODES="$QUOTA_INODES" \
PYTHONPATH="$BUNDLE/src" \
  .venv/bin/python -m pytest tests/scratch/test_f25_slice4_migrated_fs.py -p no:randomly -rs -v
EOF
RC=$?

echo "== teardown =="
cleanup
trap - EXIT
exit $RC
