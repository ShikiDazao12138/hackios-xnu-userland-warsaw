#!/usr/bin/env python3
"""Convert the iOS data partition (darwin_var) from ANNFS001 to APFS, from Android over adb.

usage: annfs-to-apfs.py [--serial S] [--backup-dir DIR] [--format]
       annfs-to-apfs.py [--serial S] --rollback BACKUP

The phone runs Android with root (su).  The files stay where they are on the partition: the
first 512 MiB of darwin_var (ANNFS001's metadata slots and its panic log; its data starts at
512 MiB) is saved to BACKUP_DIR, an APFS container is built on this machine with its metadata in
those 512 MiB and its files' extents on the existing data (apfs-tool annfs, cmd_annfs.c), and
those 512 MiB are written back.  The container covers the partition less its last 32 MiB, which
the kernel keeps for the panic log and the backlight setting; file data ANNFS has there is
copied into the first 512 MiB with the rest.  Every transfer is checked with SHA-256; a
partition that is APFS already is left alone.

--format makes the partition an empty APFS data volume instead, whatever it holds (a new
install, or starting over): every file on it is gone.  The first 512 MiB are saved first too.

--rollback writes a saved BACKUP back over the first 512 MiB: the partition is as it was before
(ANNFS001 after a conversion).  Changes made under APFS since then are lost.

Needs: adb, a C compiler with zlib and OpenSSL for apfs-tool (tools/apfs/build-tool.sh, run each
time into apfs-work/), and about 600 MiB free here and on the phone's /data.  apfsck
(tools/apfs/build-apfsck.sh, which clones linux-apfs/apfsprogs the first time) checks the new
container; without it the conversion goes on unchecked.
"""
import argparse
import datetime
import hashlib
import os
import shlex
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TOOL = os.path.join(ROOT, 'apfs-work', 'apfs-tool')
MIB = 1 << 20
HEAD = 512 * MIB
TAIL = 32 * MIB
PHONE_TMP = '/data/local/tmp/annfs-to-apfs.img'


def die(msg):
    sys.exit('annfs-to-apfs: ' + msg)


def adb(serial, *args, capture=True):
    r = subprocess.run(['adb', '-s', serial, *args], capture_output=capture, text=True)
    if r.returncode != 0:
        die('adb %s: %s' % (' '.join(args), (r.stderr or r.stdout or '').strip()))
    return r.stdout if capture else ''


def su(serial, cmd):
    return adb(serial, 'shell', 'su -c ' + shlex.quote(cmd)).strip()


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(4 * MIB), b''):
            h.update(chunk)
    return h.hexdigest()


def phone_sha(serial, what, count=None):
    """SHA-256 of a file on the phone, or of the first `count` 4 MiB blocks of a device."""
    if count is None:
        return su(serial, 'sha256sum %s' % what).split()[0]
    return su(serial, 'dd if=%s bs=4194304 count=%d 2>/dev/null | sha256sum' % (what, count)).split()[0]


def fetch(serial, dd, path, size):
    """Run `dd` (into PHONE_TMP) on the phone and bring the result here, checked."""
    su(serial, dd)
    want = phone_sha(serial, PHONE_TMP)
    adb(serial, 'pull', PHONE_TMP, path)
    su(serial, 'rm -f %s' % PHONE_TMP)
    if os.path.getsize(path) != size or sha256(path) != want:
        die('%s differs from what the phone read' % path)


def put_head(serial, dev, path):
    """Write `path` (512 MiB) over the start of the partition and read it back."""
    want = sha256(path)
    adb(serial, 'push', path, PHONE_TMP)
    if phone_sha(serial, PHONE_TMP) != want:
        die('%s did not reach the phone whole' % path)
    su(serial, 'dd if=%s of=%s bs=4194304 conv=notrunc,fsync 2>/dev/null && sync' % (PHONE_TMP, dev))
    su(serial, 'rm -f %s' % PHONE_TMP)
    if phone_sha(serial, dev, HEAD // (4 * MIB)) != want:
        die('the partition does not read back what was written; write %s back with --rollback' % path)


def find_partition(serial):
    if su(serial, 'id -u') != '0':
        die('no root on the phone (su)')
    dev = su(serial, 'readlink -f /dev/block/by-name/darwin_var')
    size = int(su(serial, 'blockdev --getsize64 %s' % dev))
    if size < 2 * HEAD:
        die('%s is %d bytes: too small for a data partition' % (dev, size))
    return dev, size


def written_past(path, off):
    """Is there anything but zeroes in the (sparse) file from `off` on?"""
    with open(path, 'rb') as f:
        while True:
            try:
                off = os.lseek(f.fileno(), off, os.SEEK_DATA)
            except OSError:                                 # ENXIO: a hole to the end
                return False
            f.seek(off)
            chunk = f.read(4 * MIB)
            if not chunk:
                return False
            if chunk.strip(b'\0'):
                return True
            off += len(chunk)


def tool():
    subprocess.run(['bash', os.path.join(HERE, 'build-tool.sh')], check=True, stdout=subprocess.DEVNULL)
    return TOOL


def apfsck():
    """apfsck's path, or None when it cannot be built here."""
    r = subprocess.run(['bash', os.path.join(HERE, 'build-apfsck.sh')], stdout=subprocess.PIPE, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def convert(serial, dev, size, backup_dir, empty_volume):
    magic = su(serial, 'dd if=%s bs=4096 count=1 2>/dev/null | od -An -c -j32 -N4' % dev).split()
    if magic == ['N', 'X', 'S', 'B'] and not empty_volume:
        print('annfs-to-apfs: %s is APFS already, nothing to do' % dev, flush=True)
        return
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
    os.makedirs(backup_dir, exist_ok=True)
    backup = os.path.join(backup_dir, 'darwin_var-head-%s.img' % stamp)
    tail = os.path.join(backup_dir, 'darwin_var-tail-%s.img' % stamp)
    print('annfs-to-apfs: saving the first 512 MiB of %s to %s' % (dev, backup), flush=True)
    fetch(serial, 'dd if=%s of=%s bs=4194304 count=%d 2>/dev/null' % (dev, PHONE_TMP, HEAD // (4 * MIB)), backup, HEAD)
    fetch(serial, 'dd if=%s of=%s bs=4096 skip=%d count=%d 2>/dev/null' % (dev, PHONE_TMP, (size - TAIL) // 4096,
                                                                          TAIL // 4096), tail, TAIL)
    with open(backup, 'rb') as f:
        first = f.read(8)
        f.seek(32 * MIB)
        second = f.read(8)
    if b'ANNFS001' not in (first, second) and not empty_volume:
        die('%s holds neither ANNFS001 nor APFS: nothing is changed (--format: an empty volume)' % dev)

    work = os.path.join(backup_dir, 'work-%s' % stamp)
    empty = os.path.join(work, 'empty')
    image = os.path.join(work, 'apfs.img')
    os.makedirs(empty)
    blocks = (size - TAIL) // 4096
    subprocess.run([sys.executable, os.path.join(HERE, 'mkapfs.py'), '-o', image, '--blocks', str(blocks),
                    'Data=%s,role=data' % empty], check=True)
    if not empty_volume:
        subprocess.run([tool(), 'annfs', image, backup, tail], check=True)
    checker = apfsck()
    if checker is not None:
        if subprocess.run([checker, '-cw', image]).returncode != 0:
            die('apfsck finds the new container unsound; nothing is changed (%s)' % work)
        print('annfs-to-apfs: apfsck finds the new container clean', flush=True)
    else:
        print('annfs-to-apfs: apfsck cannot be built here (tools/apfs/build-apfsck.sh); not checked', flush=True)
    if written_past(image, HEAD):
        die('the conversion wrote past 512 MiB; nothing is changed (%s)' % work)
    patch = os.path.join(work, 'apfs-head.img')
    with open(image, 'rb') as f, open(patch, 'wb') as out:
        out.write(f.read(HEAD))
    print('annfs-to-apfs: writing the new first 512 MiB', flush=True)
    put_head(serial, dev, patch)
    shutil.rmtree(work)
    os.unlink(tail)
    print('annfs-to-apfs: done; boot iOS.  To undo: %s --rollback %s' % (os.path.relpath(__file__), backup))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--serial', default=os.environ.get('ANDROID_SERIAL'))
    ap.add_argument('--backup-dir', default=os.environ.get('BACKUP_DIR') or os.path.join('out', 'annfs-to-apfs'))
    ap.add_argument('--rollback', metavar='BACKUP')
    ap.add_argument('--format', action='store_true', help='an empty APFS volume, whatever the partition holds')
    args = ap.parse_args()
    if not args.serial:
        die('no phone named: --serial or ANDROID_SERIAL')
    dev, size = find_partition(args.serial)
    if args.rollback:
        if os.path.getsize(args.rollback) != HEAD:
            die('%s is not a saved first 512 MiB' % args.rollback)
        put_head(args.serial, dev, args.rollback)
        print('annfs-to-apfs: %s is as it was before' % dev, flush=True)
        return
    convert(args.serial, dev, size, args.backup_dir, args.format)


if __name__ == '__main__':
    main()
