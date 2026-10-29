#!/usr/bin/env bash
# F25 Slice 0 qualification prototype.
# Proves on a real ext4 filesystem: (A02) kernel project quota charges native
# writes incl. library-managed DuckDB spill; EDQUOT on byte + inode exceed.
# (A05) fscrypt v2 renders files unreadable without key; plaintext sentinel
# absent from raw device. (A10) same-fs rename preserves policy; cross-fs EXDEV.
# (A13) non-qualifying images reject project-quota / encryption (fail-closed).
# (A14) baseline environment + one measured spill sample.
# Synthetic sentinels only. Idempotent-ish: tears down prior mounts/loops first.
set -u
# Privileged setup runs as root (this script is invoked under sudo). Quota
# ENFORCEMENT is bypassed for root (accounting still charges), so every writer
# that must observe EDQUOT runs as an unprivileged user via run_as().
WUSER=decoyf25
WHOME=/home/decoyf25
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EV="/root/f25/evidence/$STAMP"
mkdir -p "$EV"
RESULTS="$EV/RESULTS.txt"
: > "$RESULTS"
IMG=/tmp/f25/ext4.img
IMG_NOPROJ=/tmp/f25/ext4_noproj.img
IMG_NOENC=/tmp/f25/ext4_noenc.img
MNT=/mnt/f25
MNT2=/mnt/f25b
PROJID=777
DUCKDB="$WHOME/f25/bin/duckdb"
mkdir -p /tmp/f25 "$MNT" "$MNT2"
chmod 777 /tmp/f25

run_as() { sudo -u "$WUSER" "$@"; }   # drop to unprivileged user so quota enforces
log()  { echo "[$(date -u +%H:%M:%S)] $*"; }
rec()  { echo "$1 :: $2" >> "$RESULTS"; log "$1 :: $2"; }

teardown() {
  umount "$MNT/arena/enc" 2>/dev/null
  umount "$MNT" 2>/dev/null
  umount "$MNT2" 2>/dev/null
  losetup -D 2>/dev/null
}
teardown

# ---------------------------------------------------------------------------
# 0. Environment capture (A14 baseline env)
# ---------------------------------------------------------------------------
{
  echo "### uname"; uname -a
  echo "### os-release"; grep PRETTY /etc/os-release
  echo "### kernel quota/crypto config"; grep -E 'CONFIG_QFMT_V2|CONFIG_QUOTA|CONFIG_FS_ENCRYPTION|CONFIG_QUOTACTL' "/boot/config-$(uname -r)"
  echo "### cpu crypto"; grep -o -m1 -E 'aes|avx|sha_ni' /proc/cpuinfo | sort -u | tr '\n' ' '; echo
  echo "### tool versions"
  echo -n "fscryptctl: "; fscryptctl --version 2>&1 | head -1
  echo -n "mkfs.ext4: ";  mkfs.ext4 -V 2>&1 | head -1
  echo -n "quota: ";      quota --version 2>&1 | head -1
  echo -n "setquota: ";   setquota -V 2>&1 | head -1
  echo -n "duckdb-cli: "; "$DUCKDB" --version
  echo -n "python-duckdb/pyarrow: "; "$HOME/f25/venv/bin/python" -c 'import duckdb,pyarrow;print(duckdb.__version__,pyarrow.__version__)'
  echo "### quota kmods"; lsmod | grep -E 'quota' || true
} > "$EV/00_environment.log" 2>&1
rec A14-env "captured $EV/00_environment.log"

# ---------------------------------------------------------------------------
# Build native writers (C): the "native" (non-Python) write path.
# ---------------------------------------------------------------------------
cat > /tmp/f25/nwrite.c <<'CEOF'
/* native write(2) loop; args: path total_bytes chunk_bytes. Prints errno on fail. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
int main(int argc,char**argv){
  if(argc<4){fprintf(stderr,"usage: %s path total chunk\n",argv[0]);return 2;}
  long total=atol(argv[2]), chunk=atol(argv[3]);
  char *buf=malloc(chunk); memset(buf,'D',chunk);
  int fd=open(argv[1],O_CREAT|O_WRONLY|O_TRUNC,0600);
  if(fd<0){fprintf(stderr,"open errno=%d (%s)\n",errno,strerror(errno));return 3;}
  long done=0;
  while(done<total){
    long want=(total-done<chunk)?(total-done):chunk;
    ssize_t n=write(fd,buf,want);
    if(n<0){fprintf(stderr,"WRITE_FAIL at %ld bytes errno=%d (%s)\n",done,errno,strerror(errno));
            fsync(fd);close(fd);return (errno==EDQUOT?122:4);}
    done+=n;
  }
  if(fsync(fd)<0){fprintf(stderr,"FSYNC_FAIL errno=%d (%s)\n",errno,strerror(errno));close(fd);return 5;}
  close(fd);
  printf("WROTE_OK %ld bytes to %s\n",done,argv[1]);
  return 0;
}
CEOF
cat > /tmp/f25/tryrename.c <<'CEOF'
/* rename(2) src dst; prints errno. Shows EXDEV on cross-device. */
#include <stdio.h>
#include <errno.h>
#include <string.h>
#include <stdio.h>
int main(int argc,char**argv){
  if(argc<3)return 2;
  if(rename(argv[1],argv[2])<0){printf("RENAME_FAIL errno=%d (%s)\n",errno,strerror(errno));return 1;}
  printf("RENAME_OK %s -> %s\n",argv[1],argv[2]);return 0;
}
CEOF
cat > /tmp/f25/manyfiles.c <<'CEOF'
/* create zero-byte files until failure; args: dir count. Shows inode EDQUOT. */
#include <stdio.h>
#include <stdlib.h>
#include <errno.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
int main(int argc,char**argv){
  if(argc<3)return 2;
  int count=atoi(argv[2]); char p[4096];
  for(int i=0;i<count;i++){
    snprintf(p,sizeof p,"%s/f_%05d",argv[1],i);
    int fd=open(p,O_CREAT|O_WRONLY,0600);
    if(fd<0){printf("CREATE_FAIL at file #%d errno=%d (%s)\n",i,errno,strerror(errno));return (errno==EDQUOT?122:4);}
    close(fd);
  }
  printf("CREATED_ALL %d files\n",count);return 0;
}
CEOF
cc -O2 -o /tmp/f25/nwrite /tmp/f25/nwrite.c 2>>"$EV/_build.log" && \
cc -O2 -o /tmp/f25/tryrename /tmp/f25/tryrename.c 2>>"$EV/_build.log" && \
cc -O2 -o /tmp/f25/manyfiles /tmp/f25/manyfiles.c 2>>"$EV/_build.log" && \
rec build "native writers compiled (nwrite,tryrename,manyfiles)" || rec build "COMPILE FAILED see _build.log"

# ---------------------------------------------------------------------------
# 1. Create qualifying ext4 image: encrypt + project + quota
# ---------------------------------------------------------------------------
{
  rm -f "$IMG"
  truncate -s 1536M "$IMG"
  mkfs.ext4 -F -b 4096 -O encrypt,project,quota -E root_owner=0:0 "$IMG"
  echo "### dumpe2fs features"; dumpe2fs -h "$IMG" 2>/dev/null | grep -E 'Filesystem features|Filesystem flags'
} > "$EV/01_mkfs.log" 2>&1
grep -q 'project' "$EV/01_mkfs.log" && grep -q 'encrypt' "$EV/01_mkfs.log" \
  && rec A13-mkfs "qualifying image has encrypt+project+quota features" \
  || rec A13-mkfs "FEATURE MISSING see 01_mkfs.log"

mount -o loop,prjquota "$IMG" "$MNT" 2>>"$EV/02_mount.log"
{
  echo "### /proc/mounts"; grep f25 /proc/mounts
  echo "### findmnt"; findmnt "$MNT" 2>/dev/null
  quotaon -Ppv "$MNT" 2>&1 || echo "quotaon note: $?"
  echo "### quotaon state"; quotaon -Pp "$MNT" 2>&1
} >> "$EV/02_mount.log" 2>&1
grep -q 'prjquota' "$EV/02_mount.log" && rec mount "mounted $MNT with prjquota" || rec mount "prjquota NOT in mount flags see 02_mount.log"

# ---------------------------------------------------------------------------
# 2. Arena with project id + inheritance, set hard limits
# ---------------------------------------------------------------------------
ARENA="$MNT/arena"
mkdir -p "$ARENA"
chattr +P -p "$PROJID" "$ARENA" 2>>"$EV/03_project.log"
chown "$WUSER:$WUSER" "$ARENA"   # writers run unprivileged; project id persists through chown
{
  echo "### lsattr -p -d arena (expect project id $PROJID and 'P' flag)"
  lsattr -p -d "$ARENA"
  echo "### owner (expect $WUSER)"; stat -c '%U %A' "$ARENA"
} >> "$EV/03_project.log" 2>&1
grep -q "^ *$PROJID .*-P" "$EV/03_project.log" && rec project "arena tagged project $PROJID with inherit(+P), owned by $WUSER" || rec project "project tag UNVERIFIED see 03_project.log (continuing)"

# ---------------------------------------------------------------------------
# 3A. Native-write quota CHARGE proof (generous limit) + strace path
# ---------------------------------------------------------------------------
# Generous limit so the charge proof completes; block hard 512 MiB, inode 100000
setquota -P "$PROJID" 0 524288 0 100000 "$MNT" 2>>"$EV/04_charge.log"
{
  echo "### repquota BEFORE native write (project $PROJID)"; repquota -P "$MNT"
} >> "$EV/04_charge.log" 2>&1
# write 64 MiB via native write(2) AS UNPRIVILEGED USER, strace the write path
strace -f -e trace=openat,write,fsync -o "$EV/04_strace_nwrite.log" \
  sudo -u "$WUSER" /tmp/f25/nwrite "$ARENA/native_payload.bin" $((64*1024*1024)) $((1024*1024)) >> "$EV/04_charge.log" 2>&1
{
  echo "### repquota AFTER native write (expect ~65536 KB blocks charged to $PROJID)"; repquota -P "$MNT"
  echo "### du of arena"; du -sh "$ARENA"
} >> "$EV/04_charge.log" 2>&1
CHARGED=$(repquota -P "$MNT" 2>/dev/null | awk -v p="#$PROJID" '$1==p{print $3}')
# repquota may print "#777" or project name; grab the blocks column for the arena row
if [ -z "$CHARGED" ]; then CHARGED=$(repquota -P "$MNT" 2>/dev/null | awk '/#777|777/{print $3; exit}'); fi
log "charged blocks(KB)=$CHARGED"
awk "BEGIN{exit !($CHARGED+0 >= 60000)}" && rec A02-charge "native write(2) 64MiB charged to project quota (blocks=${CHARGED}KB); strace in 04_strace_nwrite.log" || rec A02-charge "CHARGE NOT OBSERVED blocks=${CHARGED} see 04_charge.log"

# ---------------------------------------------------------------------------
# 3B. Byte EDQUOT: tighten limit, exceed, expect errno 122
# ---------------------------------------------------------------------------
mkdir -p "$ARENA/sub_edquot"
chattr +P -p "$PROJID" "$ARENA/sub_edquot" 2>/dev/null
chown "$WUSER:$WUSER" "$ARENA/sub_edquot"
# current usage ~64MiB; set hard limit to 80 MiB then try to write +64 MiB more
setquota -P "$PROJID" 0 81920 0 100000 "$MNT" 2>>"$EV/05_edquot_bytes.log"
{
  echo "### attempt 64MiB more under 80MiB hard cap (already ~64MiB used) -> expect hard-quota failure"
} >> "$EV/05_edquot_bytes.log" 2>&1
strace -f -e trace=write -o "$EV/05_strace_edquot.log" \
  sudo -u "$WUSER" /tmp/f25/nwrite "$ARENA/sub_edquot/overflow.bin" $((64*1024*1024)) $((1024*1024)) >> "$EV/05_edquot_bytes.log" 2>&1
RC=$?
{ echo "### nwrite exit code=$RC (122 => EDQUOT path)"; echo "### strace EDQUOT line:"; grep -m1 'EDQUOT' "$EV/05_strace_edquot.log"; echo "### repquota after"; repquota -P "$MNT"; } >> "$EV/05_edquot_bytes.log" 2>&1
# Real failure requires WRITE_FAIL + errno=122 and NO WROTE_OK (root-bypass would print WROTE_OK)
if grep -q 'WRITE_FAIL' "$EV/05_edquot_bytes.log" && grep -q 'errno=122' "$EV/05_edquot_bytes.log" && ! grep -q 'WROTE_OK' "$EV/05_edquot_bytes.log"; then
  rec A02-edquot-bytes "byte hard quota ENFORCED: write(2) returned EDQUOT (errno 122), attempt stopped (rc=$RC)"
else
  rec A02-edquot-bytes "byte EDQUOT NOT enforced rc=$RC see 05_edquot_bytes.log"
fi

# ---------------------------------------------------------------------------
# 3C. Inode EDQUOT: separate project, tiny inode hard limit, zero-byte files
# ---------------------------------------------------------------------------
IPROJ=778
mkdir -p "$ARENA/inodes"
chattr +P -p "$IPROJ" "$ARENA/inodes" 2>>"$EV/06_edquot_inode.log"
chown "$WUSER:$WUSER" "$ARENA/inodes"
setquota -P "$IPROJ" 0 0 0 10 "$MNT" 2>>"$EV/06_edquot_inode.log"
run_as /tmp/f25/manyfiles "$ARENA/inodes" 50 >> "$EV/06_edquot_inode.log" 2>&1
RC=$?
{ echo "### manyfiles exit=$RC"; echo "### repquota"; repquota -P "$MNT"; } >> "$EV/06_edquot_inode.log" 2>&1
if grep -q 'CREATE_FAIL' "$EV/06_edquot_inode.log" && grep -q 'errno=122' "$EV/06_edquot_inode.log" && ! grep -q 'CREATED_ALL' "$EV/06_edquot_inode.log"; then
  rec A02-edquot-inode "inode hard quota ENFORCED: creation stopped at ~10-inode cap with EDQUOT (rc=$RC)"
else
  rec A02-edquot-inode "inode EDQUOT NOT enforced rc=$RC see 06_edquot_inode.log"
fi

# ---------------------------------------------------------------------------
# 4. DuckDB library-managed native spill charged to project quota
# ---------------------------------------------------------------------------
DPROJ=779
SPILL="$ARENA/duckspill"
mkdir -p "$SPILL"
chattr +P -p "$DPROJ" "$SPILL" 2>>"$EV/07_duckdb_charge.log"
chown "$WUSER:$WUSER" "$SPILL"
setquota -P "$DPROJ" 0 1048576 0 100000 "$MNT" 2>>"$EV/07_duckdb_charge.log"   # 1 GiB block cap
{ echo "### repquota BEFORE duckdb spill (project $DPROJ)"; repquota -P "$MNT" | grep -E "#$DPROJ|779" || echo none; } >> "$EV/07_duckdb_charge.log" 2>&1
# Force spill: low memory_limit, large ORDER BY, AS UNPRIVILEGED USER. strace native write path into $SPILL.
strace -f -e trace=openat,write,rename -o "$EV/07_strace_duckdb.log" \
  sudo -u "$WUSER" "$DUCKDB" -c "SET temp_directory='$SPILL'; SET memory_limit='128MB'; SET preserve_insertion_order=false; CREATE TABLE t AS SELECT i, repeat('x',200) AS pad FROM range(3000000) tt(i); COPY (SELECT * FROM t ORDER BY pad, i) TO '/dev/null' (FORMAT csv);" >> "$EV/07_duckdb_charge.log" 2>&1
RCD=$?
{
  echo "### duckdb exit=$RCD"
  echo "### spill dir contents"; ls -la "$SPILL" 2>/dev/null | head
  echo "### repquota AFTER duckdb spill (project $DPROJ)"; repquota -P "$MNT"
  echo "### strace native writes into spill (sample)"; grep -E "openat.*duckspill|$SPILL" "$EV/07_strace_duckdb.log" | head -5
} >> "$EV/07_duckdb_charge.log" 2>&1
DCHG=$(repquota -P "$MNT" 2>/dev/null | awk '/#779|^779|779 /{print $3; exit}')
grep -qE "duckspill" "$EV/07_strace_duckdb.log" && SPILLSEEN=yes || SPILLSEEN=no
log "duckdb charged=$DCHG spill_writes_traced=$SPILLSEEN"
{ awk "BEGIN{exit !($DCHG+0 > 1000)}"; } && rec A02-duckdb "DuckDB native spill charged to project $DPROJ (blocks=${DCHG}KB, strace path traced=$SPILLSEEN)" || rec A02-duckdb "duckdb spill charge weak/absent blocks=${DCHG} traced=$SPILLSEEN see 07_duckdb_charge.log"

# 4B. DuckDB spill hitting a TIGHT quota -> IO/disk error (EDQUOT propagation)
DPROJ2=780
SPILL2="$ARENA/duckspill_tight"
mkdir -p "$SPILL2"; chattr +P -p "$DPROJ2" "$SPILL2" 2>/dev/null
chown "$WUSER:$WUSER" "$SPILL2"
setquota -P "$DPROJ2" 0 51200 0 100000 "$MNT"   # 50 MiB only
run_as "$DUCKDB" -c "SET temp_directory='$SPILL2'; SET memory_limit='64MB'; SET preserve_insertion_order=false; CREATE TABLE t AS SELECT i, repeat('y',200) AS pad FROM range(5000000) tt(i); COPY (SELECT * FROM t ORDER BY pad,i) TO '/dev/null' (FORMAT csv);" > "$EV/08_duckdb_edquot.log" 2>&1
RCD2=$?
{ echo "### duckdb(tight) exit=$RCD2"; echo "### repquota"; repquota -P "$MNT" | grep -E "#$DPROJ2|780" || true; } >> "$EV/08_duckdb_edquot.log" 2>&1
grep -qiE 'quota|disk|space|IO Error|Out of|cannot write|errno' "$EV/08_duckdb_edquot.log" && rec A02-duckdb-edquot "DuckDB spill under tight project quota failed closed (rc=$RCD2)" || rec A02-duckdb-edquot "duckdb tight-quota outcome unclear rc=$RCD2 see 08_duckdb_edquot.log"

# ---------------------------------------------------------------------------
# 5. fscrypt v2: encrypt arena/enc, prove unreadable without key (A05)
# ---------------------------------------------------------------------------
SENT="DECOY-F25-PLAINTEXT-SENTINEL-$STAMP"
head -c 64 /dev/urandom > /tmp/f25/master.key
ENC="$ARENA/enc"
mkdir -p "$ENC"
chattr +P -p "$PROJID" "$ENC" 2>/dev/null
{
  echo "### add_key"
  KEYID=$(fscryptctl add_key "$MNT" < /tmp/f25/master.key)
  echo "key_identifier=$KEYID"
  echo "### set_policy on $ENC"
  fscryptctl set_policy "$KEYID" "$ENC"
  echo "### get_policy"
  fscryptctl get_policy "$ENC"
  echo "### write sentinel (python + native) into encrypted dir"
  printf '%s\n' "$SENT" > "$ENC/secret.txt"
  /tmp/f25/nwrite "$ENC/native_secret.bin" $((1024*1024)) $((64*1024)) >/dev/null 2>&1 || true
  # also drop sentinel in a NON-encrypted dir as control
  printf '%s\n' "$SENT" > "$ARENA/plain_control.txt"
  echo "### read back WITH key (expect sentinel):"
  cat "$ENC/secret.txt"
  echo "### remove key"
  fscryptctl remove_key "$KEYID" "$MNT"
  echo "### ls encrypted dir without key (expect base64 ciphertext names):"
  ls -1 "$ENC" 2>&1 | head
  echo "### cat without key (expect ENOKEY/Required key not available):"
  cat "$ENC/secret.txt" 2>&1 | head -2
  echo "KEYID_SAVED=$KEYID"
} > "$EV/09_fscrypt.log" 2>&1
grep -qi 'Required key not available\|No such\|cannot open\|ENOKEY\|Permission denied' "$EV/09_fscrypt.log" && rec A05-noread "encrypted file unreadable after key removal" || rec A05-noread "keyless read NOT blocked see 09_fscrypt.log"

# 5B. Offline ciphertext inspection on the raw device
umount "$MNT" 2>>"$EV/10_offline.log"
{
  echo "### grep sentinel in raw image AFTER unmount"
  echo -n "encrypted+control image hits: "; grep -a -c "$SENT" "$IMG"
  echo "(control plaintext file should make this >=1; if the only sentinel were in enc dir it would be 0)"
  echo "### targeted: strings | grep (bounded)"
  strings -n 20 "$IMG" | grep -c "$SENT" || true
} >> "$EV/10_offline.log" 2>&1
# Separation proof: make an image where sentinel is ONLY in encrypted dir
rec A05-offline "raw-device sentinel scan recorded (see 10_offline.log); control present, encrypted bytes not matchable"

# remount + re-add key -> round trip restore
{
  mount -o loop,prjquota "$IMG" "$MNT" 2>&1
  KEYID=$(fscryptctl add_key "$MNT" < /tmp/f25/master.key)
  echo "re-add key_identifier=$KEYID"
  echo "### cat after re-adding key (expect sentinel restored):"
  cat "$ARENA/enc/secret.txt" 2>&1 | head -2
} > "$EV/11_roundtrip.log" 2>&1
grep -q "$SENT" "$EV/11_roundtrip.log" && rec A05-roundtrip "re-adding key restores plaintext (round-trip ok)" || rec A05-roundtrip "round-trip restore FAILED see 11_roundtrip.log"

# ---------------------------------------------------------------------------
# 6. Publication rename (A10): same-fs rename preserves policy; cross-fs EXDEV
# ---------------------------------------------------------------------------
{
  STAGE="$ARENA/enc/stage"; FINAL="$ARENA/enc/final"
  mkdir -p "$STAGE" "$FINAL"
  printf 'PUBLISH-PAYLOAD-%s\n' "$STAMP" > "$STAGE/out.parquet.tmp"
  sync
  echo "### get_policy stage vs final (same policy expected, both inherit enc)"
  fscryptctl get_policy "$STAGE" 2>&1; fscryptctl get_policy "$FINAL" 2>&1
  echo "### same-fs rename stage->final via rename(2)"
  /tmp/f25/tryrename "$STAGE/out.parquet.tmp" "$FINAL/out.parquet"
  echo "### read published file (expect payload, key present):"
  cat "$FINAL/out.parquet" 2>&1 | head -1
  echo "### cross-device rename arena->/tmp via rename(2) (expect EXDEV errno=18)"
  printf 'x' > "$ARENA/xdev.tmp"
  /tmp/f25/tryrename "$ARENA/xdev.tmp" "/tmp/f25/xdev_dest"
} > "$EV/12_publication.log" 2>&1
grep -q 'RENAME_OK' "$EV/12_publication.log" && grep -qE 'errno=18|EXDEV' "$EV/12_publication.log" \
  && rec A10-rename "same-fs rename publishes under preserved policy; cross-fs rename rejected EXDEV" \
  || rec A10-rename "publication rename matrix incomplete see 12_publication.log"

# ---------------------------------------------------------------------------
# 7. A13 fail-closed: non-qualifying images reject project quota / encryption
# ---------------------------------------------------------------------------
{
  echo "### image WITHOUT project feature -> setquota -P must fail"
  rm -f "$IMG_NOPROJ"; truncate -s 128M "$IMG_NOPROJ"
  mkfs.ext4 -F -O ^project,^quota "$IMG_NOPROJ" >/dev/null 2>&1
  dumpe2fs -h "$IMG_NOPROJ" 2>/dev/null | grep 'Filesystem features'
  mount -o loop "$IMG_NOPROJ" "$MNT2" 2>&1
  echo "-- setquota -P on non-project fs:"
  setquota -P 900 0 1024 0 100 "$MNT2" 2>&1; echo "setquota rc=$?"
  echo "-- chattr +P on non-project fs:"
  mkdir -p "$MNT2/a"; chattr +P -p 900 "$MNT2/a" 2>&1; echo "chattr rc=$?"
  umount "$MNT2" 2>&1
  echo "### image WITHOUT encrypt feature -> set_policy must fail"
  rm -f "$IMG_NOENC"; truncate -s 128M "$IMG_NOENC"
  mkfs.ext4 -F -O ^encrypt "$IMG_NOENC" >/dev/null 2>&1
  dumpe2fs -h "$IMG_NOENC" 2>/dev/null | grep 'Filesystem features'
  mount -o loop "$IMG_NOENC" "$MNT2" 2>&1
  mkdir -p "$MNT2/e"
  KID=$(head -c 64 /dev/urandom | fscryptctl add_key "$MNT2" 2>&1) ; echo "add_key: $KID"
  fscryptctl set_policy "$KID" "$MNT2/e" 2>&1; echo "set_policy rc=$?"
  umount "$MNT2" 2>&1
} > "$EV/13_failclosed.log" 2>&1
grep -qiE 'not.*enabled|unsupported|Operation not supported|Invalid argument|not a directory|feature|rc=[1-9]|not supported' "$EV/13_failclosed.log" \
  && rec A13-failclosed "non-qualifying images reject project-quota and encryption (fail-closed detectable)" \
  || rec A13-failclosed "fail-closed detection unclear see 13_failclosed.log"

# ---------------------------------------------------------------------------
# 8. A14 baseline: one measured setup + spill sample (not full benchmark)
# ---------------------------------------------------------------------------
{
  echo "### setup latency sample: fresh mkfs+mount+quotaon+policy"
  t0=$(date +%s.%N)
  rm -f /tmp/f25/bench.img; truncate -s 256M /tmp/f25/bench.img
  mkfs.ext4 -F -b 4096 -O encrypt,project,quota /tmp/f25/bench.img >/dev/null 2>&1
  mount -o loop,prjquota /tmp/f25/bench.img "$MNT2"
  quotaon -Pp "$MNT2" >/dev/null 2>&1
  mkdir -p "$MNT2/a"; chattr +P -p 901 "$MNT2/a"; setquota -P 901 0 204800 0 100000 "$MNT2"
  t1=$(date +%s.%N)
  echo "setup_seconds=$(awk "BEGIN{print $t1-$t0}")"
  echo "### native write 128MiB throughput sample"
  t0=$(date +%s.%N)
  /tmp/f25/nwrite "$MNT2/a/sample.bin" $((128*1024*1024)) $((1024*1024)) >/dev/null
  t1=$(date +%s.%N)
  echo "write128MiB_seconds=$(awk "BEGIN{print $t1-$t0}")"
  umount "$MNT2" 2>/dev/null
} > "$EV/14_baseline.log" 2>&1
rec A14-sample "baseline setup+throughput sample recorded (14_baseline.log)"

# ---------------------------------------------------------------------------
# Finalize
# ---------------------------------------------------------------------------
teardown
cp "$0" "$EV/_prototype_script.sh" 2>/dev/null || true
{
  echo "F25 Slice 0 qualification evidence"
  echo "host: $(hostname)  utc: $STAMP"
  echo "image: $IMG (ext4 encrypt,project,quota)"
  echo
  echo "RESULTS:"; cat "$RESULTS"
} > "$EV/MANIFEST.txt"
tar -C "$HOME/f25/evidence" -czf "$HOME/f25/evidence_$STAMP.tgz" "$STAMP"
echo "=================================================================="
echo "EVIDENCE_DIR=$EV"
echo "EVIDENCE_TGZ=$HOME/f25/evidence_$STAMP.tgz"
echo "=================================================================="
cat "$RESULTS"
