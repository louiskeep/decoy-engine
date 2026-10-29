#!/usr/bin/env bash
# F25 Slice 1 A02 real-kernel-quota test driver (GCP qualifying host).
#
# Why this exists: the project-quota guarantee only holds for an UNPRIVILEGED
# writer (root is quota-exempt), and only on an ext4 mount with project quota
# enforced. So the privileged setup (mkfs/mount/quotaon/setquota/chattr +P) runs
# as root here, and the writers run as the unprivileged user. Running the whole
# thing as root would give a false pass (Slice-0 report condition 1).
#
# Usage (on the GCP host, as a user with sudo):
#   scripts/f25/run_slice1_fs_tests.sh <bundle_dir> [wuser]
#
# <bundle_dir> holds a minimal importable tree: decoy_engine/{__init__.py,
# errors.py, scratch/} plus test_quota_enforcement_fs.py. The scratch package is
# pure stdlib, so the test venv needs only pytest. The script provisions a
# loop-mounted ext4 image with a small project quota, hands the arena root to the
# unprivileged user, and runs the suite as that user.
set -u
BUNDLE="${1:?usage: run_slice1_fs_tests.sh <bundle_dir> [wuser]}"
WUSER="${2:-decoyf25}"
IMG=/tmp/f25/slice1.img
MNT=/mnt/f25
PROJID=911                           # data project (small caps)
CPROJID=912                          # control project (generous, separate domain)
QUOTA_BYTES=$((8 * 1024 * 1024))     # 8 MiB hard byte cap
QUOTA_KB=$((QUOTA_BYTES / 1024))
QUOTA_INODES=64
ARENA="$MNT/slice1-arena"
CONTROL="$MNT/slice1-control"

if [ "$(id -u)" -ne 0 ]; then
  echo "provisioning needs root; re-run under sudo" >&2
  exit 2
fi

echo "== provisioning ext4 + project quota (root) =="
mkdir -p /tmp/f25 "$MNT"
umount "$MNT" 2>/dev/null
losetup -D 2>/dev/null
rm -f "$IMG"
truncate -s 256M "$IMG"
mkfs.ext4 -F -b 4096 -O encrypt,project,quota "$IMG" >/dev/null 2>&1
mount -o loop,prjquota "$IMG" "$MNT"
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

echo "== preparing test venv (unprivileged) =="
sudo -u "$WUSER" bash -s <<EOF
set -e
cd "$BUNDLE"
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -q --disable-pip-version-check pytest >/dev/null 2>&1 || true
echo "== running A02 as \$(whoami) (must NOT be root) =="
DECOY_F25_QUOTA_ROOT="$ARENA" \
DECOY_F25_CONTROL_ROOT="$CONTROL" \
DECOY_F25_QUOTA_BYTES="$QUOTA_BYTES" \
DECOY_F25_QUOTA_INODES="$QUOTA_INODES" \
PYTHONPATH="$BUNDLE" \
  .venv/bin/python -m pytest test_quota_enforcement_fs.py -p no:randomly -rs -v
EOF
RC=$?

echo "== teardown =="
umount "$MNT" 2>/dev/null
losetup -D 2>/dev/null
exit $RC
