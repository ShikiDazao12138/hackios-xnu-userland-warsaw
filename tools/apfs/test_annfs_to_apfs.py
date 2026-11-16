#!/usr/bin/env python3
"""annfs-to-apfs.py from end to end, with a stand-in adb that runs the phone's commands on files here.

The phone's darwin_var is a 1 GiB sparse file; /dev/block/by-name/darwin_nvram exists or not. Cases:
an ANNFS001 partition (test_annfs.py's) converted without darwin_nvram, where the container leaves
out the last 32 MiB, and with it, where the container covers the partition; --format on a new
install with darwin_nvram; and a partition that is APFS already. Every converted partition is read
back object by object as test_annfs.py does and checked with apfsck when it can be built.

usage: test_annfs_to_apfs.py [-k]
"""
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_annfs                                               # noqa: E402

SCRIPT = os.path.join(HERE, 'annfs-to-apfs.py')
MIB = 1 << 20
PART = test_annfs.PART

FAKE_ADB = r'''#!/usr/bin/env python3
import os, shlex, shutil, subprocess, sys
phone = os.environ['FAKE_PHONE']
args = sys.argv[3:]                                             # after -s SERIAL
here = lambda p: p.replace('/data/local/tmp/', phone + '/tmp/')
if args[0] in ('pull', 'push'):
    shutil.copyfile(here(args[1]), here(args[2]))
    sys.exit(0)
cmd = shlex.split(args[1])[2]                                   # shell 'su -c CMD'
for a, b in (('readlink -f /dev/block/by-name/darwin_var', 'echo ' + phone + '/by-name/darwin_var'),
             ('/dev/block/by-name/', phone + '/by-name/'), ('blockdev --getsize64', 'stat -f %z'),
             ('sha256sum', 'shasum -a 256'), ('conv=notrunc,fsync', 'conv=notrunc'), ('id -u', 'echo 0')):
    cmd = cmd.replace(a, b)
sys.exit(subprocess.run(['bash', '-c', here(cmd)]).returncode)
'''


def phone(work, nvram):
    d = os.path.join(work, 'phone')
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.join(d, 'by-name'))
    os.makedirs(os.path.join(d, 'tmp'))
    with open(os.path.join(d, 'by-name', 'darwin_var'), 'wb') as f:
        f.truncate(PART)
    if nvram:
        open(os.path.join(d, 'by-name', 'darwin_nvram'), 'wb').close()
    return d


def run(work, d, *args):
    bin_dir = os.path.join(work, 'bin')
    env = dict(os.environ, FAKE_PHONE=d, PATH=bin_dir + os.pathsep + os.environ['PATH'])
    return subprocess.run([sys.executable, SCRIPT, '--serial', 'fake', '--backup-dir', os.path.join(work, 'backup'),
                           *args], env=env, capture_output=True, text=True)


def container_blocks(part):
    with open(part, 'rb') as f:
        block0 = f.read(4096)
    if block0[32:36] != b'NXSB':
        return None
    return struct.unpack_from('<Q', block0, 40)[0]


def apfsck(part):
    checker = subprocess.run(['bash', os.path.join(HERE, 'build-apfsck.sh')], stdout=subprocess.PIPE, text=True)
    if checker.returncode != 0:
        return []
    ck = subprocess.run([checker.stdout.strip(), '-cw', part], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return [] if ck.returncode == 0 else ['apfsck: ' + ck.stdout.decode('utf-8', 'replace').strip()[-600:]]


def convert(work, nvram):
    d = phone(work, nvram)
    part = os.path.join(d, 'by-name', 'darwin_var')
    r = random.Random(7)
    objs = test_annfs.make_tree(r, 400)
    test_annfs.write_partition(part, objs, r)
    res = run(work, d)
    if res.returncode != 0:
        return ['annfs-to-apfs: ' + (res.stderr or res.stdout).strip()[-800:]]
    want = (PART - (0 if nvram else test_annfs.TAIL)) // 4096
    problems = [] if container_blocks(part) == want else ['container of %s blocks, not %d' % (container_blocks(part), want)]
    return problems + test_annfs.check(part, objs) + apfsck(part)


def new_install(work):
    d = phone(work, True)
    part = os.path.join(d, 'by-name', 'darwin_var')
    res = run(work, d, '--format')
    if res.returncode != 0:
        return ['annfs-to-apfs --format: ' + (res.stderr or res.stdout).strip()[-800:]]
    problems = [] if container_blocks(part) == PART // 4096 else ['container of %s blocks' % container_blocks(part)]
    again = run(work, d)
    if again.returncode != 0 or 'APFS already' not in again.stdout:
        problems.append('a second run does not leave the APFS partition alone: ' + again.stdout + again.stderr)
    return problems + apfsck(part)


def main():
    work = tempfile.mkdtemp(prefix='annfs-to-apfs-')
    os.makedirs(os.path.join(work, 'bin'))
    adb = os.path.join(work, 'bin', 'adb')
    with open(adb, 'w') as f:
        f.write(FAKE_ADB)
    os.chmod(adb, 0o755)
    failed = 0
    for name, case in (('ANNFS001 without darwin_nvram', lambda: convert(work, False)),
                       ('ANNFS001 with darwin_nvram', lambda: convert(work, True)),
                       ('new install with darwin_nvram (--format), then again', lambda: new_install(work))):
        problems = case()
        print('%s: %s' % (name, 'ok' if not problems else '%d problems' % len(problems)), flush=True)
        for p in problems[:20]:
            print('  ' + p)
        failed += bool(problems)
    if '-k' in sys.argv[1:]:
        print('work directory: ' + work)
    else:
        shutil.rmtree(work)
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
