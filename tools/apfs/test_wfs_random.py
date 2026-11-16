#!/usr/bin/env python3
"""Random operations through the kernel's APFS writer, checked against a model of the tree.

Each round builds a fresh image with mkapfs.py, generates a run of operations that are valid
for the model (create, mkdir, symlink, mknod, write, truncate, link, unlink, rmdir, rename
with replace and RENAME_SWAP, xattrs small and in their own streams, chmod, chown, commits),
runs them with `apfs-tool do`, and then holds the image to:

  apfsck -cw          linux-apfs/apfsprogs: every tree, the space manager, every count
  apfs.py             this repository's reader: names, types, modes, owners, sizes, contents,
                      attributes, link targets and link counts equal the model's

The first round is also run once per device write with the device cut off after that write
(--no-crash skips it): what is on the image must be exactly the model as of a commit that
completed, and apfsck must find it sound.

usage: test_wfs_random.py [--rounds N] [--ops N] [--seed S] [--big] [--no-crash] [-k]
"""
import argparse
import os
import random
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import apfs                                                     # noqa: E402

TOOL = os.path.join(ROOT, 'apfs-work', 'apfs-tool')
APFSCK = os.path.join(ROOT, 'xnu-deps', 'build', 'apfsprogs', 'apfsck', 'apfsck')
BS = 4096
M64 = (1 << 64) - 1


def gen_bytes(seed, n):
    """The same generator as cmd_wfs.c: xorshift64*, eight little-endian bytes a step."""
    x = seed or 0x9E3779B97F4A7C15
    out = bytearray()
    while len(out) < n:
        x ^= x >> 12
        x ^= (x << 25) & M64
        x ^= x >> 27
        out += ((x * 0x2545F4914F6CDD1D) & M64).to_bytes(8, 'little')
    return bytes(out[:n])


SERIAL = [0]


class Obj:
    def __init__(self, kind, mode, target=None, rdev=0):
        SERIAL[0] += 1
        self.serial = SERIAL[0]
        self.kind = kind            # 'f' 'd' 'l' 'p' 'c'
        self.mode = mode
        self.uid = 0
        self.gid = 0
        self.data = bytearray()
        self.xattrs = {}
        self.target = target
        self.rdev = rdev
        self.children = {} if kind == 'd' else None
        self.links = 1


class Model:
    def __init__(self):
        self.root = Obj('d', 0o755)
        self.root.children['a'] = Obj('f', 0o644)
        self.root.children['a'].data = bytearray(b'hello\n')
        self.root.children['d'] = Obj('d', 0o755)

    def walk(self, d=None, path=''):
        d = d or self.root
        for name, o in sorted(d.children.items()):
            p = path + '/' + name
            yield p, o, d, name
            if o.kind == 'd':
                yield from self.walk(o, p)

    def entries(self):
        return list(self.walk())

    def dirs(self):
        return [('', self.root)] + [(p, o) for p, o, _, _ in self.walk() if o.kind == 'd']

    def snapshot(self):
        out = {}
        for p, o, _, _ in self.walk():
            out[p] = (o.kind, o.mode, o.uid, o.gid, bytes(o.data), dict(o.xattrs), o.target,
                      o.links if o.kind != 'd' else None, o.rdev, o.serial)
        return out


def deep_copy(o):
    """What clonefile makes of o: the same contents in objects of its own."""
    c = Obj(o.kind, o.mode, target=o.target, rdev=o.rdev)
    c.uid, c.gid = o.uid, o.gid
    c.data = bytearray(o.data)
    c.xattrs = dict(o.xattrs)
    if o.kind == 'd':
        for n, ch in o.children.items():
            c.children[n] = deep_copy(ch)
    return c


def below(model, d, target):
    """Is directory object target d itself or inside it?"""
    if d is target:
        return True
    for _, o, _, _ in model.walk(d):
        if o is target:
            return True
    return False


class Gen:
    def __init__(self, rng, model, big):
        self.rng = rng
        self.m = model
        self.big = big
        self.n = 0
        self.touched = set()        # objects whose data blocks an operation wrote in place

    def name(self):
        self.n += 1
        r = self.rng.random()
        if r < 0.1:
            return 'Ünïcødé-%d' % self.n
        if r < 0.15:
            return ('long-%d-' % self.n) + 'x' * self.rng.randint(100, 200)
        return 'n%d' % self.n

    def pick_dir(self):
        return self.rng.choice(self.m.dirs())

    def op(self):
        r = self.rng
        m = self.m
        ents = m.entries()
        files = [(p, o, d, n) for p, o, d, n in ents if o.kind == 'f']
        choice = r.choices(['create', 'mkdir', 'symlink', 'mknod', 'write', 'truncate', 'link',
                            'unlink', 'rmdir', 'rename', 'setxattr', 'rmxattr', 'chmod', 'chown', 'clone'],
                           weights=[12, 5, 3, 1, 14, 4, 3, 6, 2, 6, 5, 2, 2, 2, 2])[0]
        if choice == 'create':
            dp, d = self.pick_dir()
            nm = self.name()
            mode = r.choice([0o644, 0o600, 0o755])
            d.children[nm] = Obj('f', mode)
            return 'create %s/%s 0%o' % (dp, nm, mode)
        if choice == 'mkdir':
            dp, d = self.pick_dir()
            nm = self.name()
            d.children[nm] = Obj('d', 0o755)
            return 'mkdir %s/%s 0755' % (dp, nm)
        if choice == 'symlink':
            dp, d = self.pick_dir()
            nm = self.name()
            t = 'target-%d/%s' % (self.n, 'y' * r.randint(1, 60))
            d.children[nm] = Obj('l', 0o755, target=t)
            return 'symlink %s/%s %s' % (dp, nm, t)
        if choice == 'mknod':
            dp, d = self.pick_dir()
            nm = self.name()
            if r.random() < 0.5:
                d.children[nm] = Obj('p', 0o644)
                return 'mknod %s/%s 010644 0' % (dp, nm)
            d.children[nm] = Obj('c', 0o644, rdev=0x1234)
            return 'mknod %s/%s 020644 0x1234' % (dp, nm)
        if choice in ('write', 'truncate') and files:
            p, o, _, _ = r.choice(files)
            self.touched.add(o.serial)
            if choice == 'truncate':
                size = r.choice([0, r.randint(0, len(o.data) + 1), len(o.data) + r.randint(1, 3 * BS),
                                 r.randint(0, 40 * BS)])
                if size < len(o.data):
                    del o.data[size:]
                else:
                    o.data += b'\0' * (size - len(o.data))
                return 'truncate %s %d' % (p, size)
            big = self.big and r.random() < 0.3
            off = r.choice([0, len(o.data), r.randint(0, len(o.data) + 2 * BS),
                            r.randint(0, 64 * BS)])
            n = r.choice([1, 7, BS - 1, BS, BS + 1, r.randint(1, 5 * BS)])
            if big:
                n = r.randint(1, 2048) * BS + r.randint(0, BS)
            seed = r.randint(1, 1 << 40)
            data = gen_bytes(seed, n)
            if off > len(o.data):
                o.data += b'\0' * (off - len(o.data))
            o.data[off:off + n] = data
            return 'write %s %d %d %d' % (p, off, seed, n)
        if choice == 'link' and files:
            p, o, _, _ = r.choice(files)
            dp, d = self.pick_dir()
            nm = self.name()
            d.children[nm] = o
            o.links += 1
            return 'link %s %s/%s' % (p, dp, nm)
        if choice == 'unlink':
            cand = [(p, o, d, n) for p, o, d, n in ents if o.kind != 'd']
            if cand:
                p, o, d, n = r.choice(cand)
                del d.children[n]
                o.links -= 1
                return 'unlink %s' % p
        if choice == 'rmdir':
            cand = [(p, o, d, n) for p, o, d, n in ents if o.kind == 'd' and not o.children]
            if cand:
                p, o, d, n = r.choice(cand)
                del d.children[n]
                return 'rmdir %s' % p
        if choice == 'rename' and ents:
            p, o, d, n = r.choice(ents)
            if r.random() < 0.3:
                # onto an existing name: same kind, an empty directory for a directory
                cand = [(p2, o2, d2, n2) for p2, o2, d2, n2 in ents
                        if o2 is not o and (o2.kind == 'd') == (o.kind == 'd') and
                        (o2.kind != 'd' or not o2.children) and not (o.kind == 'd' and below(m, o, d2))]
                if cand:
                    p2, o2, d2, n2 = r.choice(cand)
                    if d2.children.get(n2) is o:
                        return None
                    if r.random() < 0.3 and o.kind != 'd' and o2.kind != 'd' and o.links == 1 \
                            and o2.links == 1:
                        d.children[n], d2.children[n2] = o2, o
                        return 'rename %s %s swap' % (p, p2)
                    if o2.kind != 'd':
                        o2.links -= 1
                    del d.children[n]
                    d2.children[n2] = o
                    return 'rename %s %s' % (p, p2)
            dp, d2 = self.pick_dir()
            if o.kind == 'd' and below(m, o, d2):
                return None
            nm = self.name()
            del d.children[n]
            d2.children[nm] = o
            return 'rename %s %s/%s' % (p, dp, nm)
        if choice == 'setxattr' and ents:
            p, o, _, _ = r.choice(ents)
            if o.kind == 'l':
                return None
            nm = 'user.x%d' % r.randint(0, 4)
            n = r.choice([0, 1, 100, 3804, 3805, r.randint(4000, 20000)])
            seed = r.randint(1, 1 << 40)
            o.xattrs[nm] = gen_bytes(seed, n)
            return 'setxattr %s %s %d %d' % (p, nm, seed, n)
        if choice == 'rmxattr' and ents:
            cand = [(p, o) for p, o, _, _ in ents if o.xattrs]
            if cand:
                p, o = r.choice(cand)
                nm = r.choice(sorted(o.xattrs))
                del o.xattrs[nm]
                return 'rmxattr %s %s' % (p, nm)
        if choice == 'chmod' and ents:
            p, o, _, _ = r.choice(ents)
            if o.kind == 'l':
                return None
            o.mode = r.choice([0o600, 0o640, 0o755, 0o4755])
            return 'chmod %s 0%o' % (p, o.mode)
        if choice == 'clone' and ents:
            p, o, _, _ = r.choice(ents)
            dp, d2 = self.pick_dir()
            if o.kind == 'd' and below(m, o, d2):
                return None
            nm = self.name()
            d2.children[nm] = deep_copy(o)
            return 'clone %s %s/%s' % (p, dp, nm)
        if choice == 'chown' and ents:
            p, o, _, _ = r.choice(ents)
            o.uid, o.gid = r.choice([(0, 0), (501, 501), (33, 20)])
            return 'chown %s %d %d' % (p, o.uid, o.gid)
        return None


def image_state(image):
    """What the image says, in the model's shape (apfs.py: a reader that is not the writer)."""
    c = apfs.Container(image)
    v = apfs.pick_volume(c, None)
    out = {}
    for p, name, fid, dt in apfs.walk(v, apfs.ROOT_DIR_INO_NUM):
        ino = v.inode(fid)
        kind = {apfs.S_IFREG: 'f', apfs.S_IFDIR: 'd', apfs.S_IFLNK: 'l', apfs.S_IFIFO: 'p',
                apfs.S_IFCHR: 'c'}[ino.ifmt]
        xattrs = {}
        for xn in ino.xattrs:
            if xn.startswith('com.apple.fs.'):
                continue
            xattrs[xn] = v.xattr_bytes(fid, xn, ino.xattrs)
        target = None
        if kind == 'l':
            t = ino.symlink_target_bytes()
            target = t.decode() if t is not None else None
        data = bytes(ino.contents()) if kind == 'f' else b''
        out[p] = (kind, ino.mode & 0o7777, ino.owner, ino.group, data, xattrs, target,
                  ino.nlink if kind != 'd' else None, ino.rdev, None)
    c.close()
    return out


def apfsck(image, crashed=False):
    # after a power cut block zero may lag the live checkpoint: -c would call that an error
    r = subprocess.run([APFSCK, '-w' if crashed else '-cw', image], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    return r.returncode, r.stdout.decode('utf-8', 'replace')


def make_base(work, big):
    tree = os.path.join(work, 'tree')
    os.makedirs(os.path.join(tree, 'd'))
    with open(os.path.join(tree, 'a'), 'wb') as f:
        f.write(b'hello\n')
    base = os.path.join(work, 'base.apfs')
    # more than one chunk of 32768 blocks: the space manager's multi-chunk paths are taken
    slack = 300000 if big else 70000
    subprocess.run([sys.executable, os.path.join(HERE, 'mkapfs.py'), '-o', base, '--slack', str(slack),
                    'data=' + tree], check=True, stdout=subprocess.DEVNULL)
    return base


def clone(src, dst):
    if os.path.exists(dst):
        os.unlink(dst)
    if sys.platform == 'darwin':
        subprocess.run(['cp', '-c', src, dst], check=True)
    else:
        shutil.copyfile(src, dst)


def diff(want, got, loose=frozenset()):
    """loose: objects whose bytes are not compared, only their length."""
    lines = []
    for p in sorted(set(want) | set(got)):
        a, b = want.get(p), got.get(p)
        if a is not None and b is not None:
            a = a[:9]
            b = b[:9]
            if want[p][9] in loose and len(a[4]) == len(b[4]):
                a = a[:4] + (b[4],) + a[5:]
        if a == b:
            continue
        if a is None or b is None:
            lines.append('%s: %s' % (p, 'missing from the image' if b is None else 'not in the model'))
            continue
        names = ['kind', 'mode', 'uid', 'gid', 'data', 'xattrs', 'target', 'links', 'rdev']
        for i, nm in enumerate(names):
            if a[i] != b[i]:
                if nm == 'data':
                    lines.append('%s: data %d bytes, image %d bytes, first difference at %s' % (
                        p, len(a[i]), len(b[i]),
                        next((k for k in range(min(len(a[i]), len(b[i]))) if a[i][k] != b[i][k]), '-')))
                elif nm == 'xattrs':
                    lines.append('%s: xattrs %s / image %s' % (p, {k: len(x) for k, x in a[i].items()},
                                                             {k: len(x) for k, x in b[i].items()}))
                else:
                    lines.append('%s: %s %r / image %r' % (p, nm, a[i], b[i]))
    return lines


def run_round(work, base, rng, nops, big, commit_every):
    model = Model()
    gen = Gen(rng, model, big)
    script, snaps = [], [model.snapshot()]
    while len(script) < nops:
        line = gen.op()
        if line is None:
            continue
        script.append(line)
        if rng.random() < 1.0 / commit_every:
            script.append('commit')
            snaps.append(model.snapshot())
    script.append('commit')
    snaps.append(model.snapshot())
    # A file's own blocks are written in place, not copied: after a power cut a file the run
    # wrote to may hold newer bytes than the checkpoint describes.  Its length may not differ.
    snaps = [(st, frozenset(gen.touched)) for st in snaps]
    sp = os.path.join(work, 'script.txt')
    with open(sp, 'w') as f:
        f.write('\n'.join(script) + '\n')
    image = os.path.join(work, 'round.apfs')
    clone(base, image)
    r = subprocess.run([TOOL, 'do', image, sp], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = r.stdout.decode('utf-8', 'replace')
    bad = [ln for ln in out.splitlines() if ln[:1].isdigit() and not ln.split()[2].startswith('ok')]
    return image, sp, script, snaps, model, r.returncode, out, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rounds', type=int, default=8)
    ap.add_argument('--ops', type=int, default=300)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--big', action='store_true', help='large writes: multi-level trees and many chunks')
    ap.add_argument('--no-crash', action='store_true', help='skip cutting the power at every write of round 0')
    ap.add_argument('-k', action='store_true', help='keep the work directory')
    args = ap.parse_args()

    work = tempfile.mkdtemp(prefix='wfs-random-')
    failures = 0
    try:
        base = make_base(work, args.big)
        for rnd in range(args.rounds):
            seed = args.seed * 1000 + rnd
            rng = random.Random(seed)
            image, sp, script, snaps, model, rc, out, bad = run_round(work, base, rng, args.ops, args.big, 25)
            problems = []
            if rc != 0 or bad:
                problems.append('apfs-tool: exit %d; %s' % (rc, '; '.join(bad[:5]) or out[-400:]))
            ck, ckout = apfsck(image)
            if ck != 0:
                problems.append('apfsck: ' + ckout.strip()[-600:])
            try:
                d = diff(snaps[-1][0], image_state(image))
            except Exception as e:                  # noqa: BLE001
                d = ['reader: %r' % e]
            problems += d[:10]
            stats = [ln for ln in out.splitlines() if ln.startswith('commits')]
            status = 'ok' if not problems else 'FAIL'
            print('[%s] round %d seed %d: %d ops %s' % (status, rnd, seed, len(script),
                                                          stats[0] if stats else ''))
            for p in problems:
                print('    ' + p)
            if problems:
                failures += 1
                keep = os.path.join(work, 'fail-%d' % seed)
                os.makedirs(keep, exist_ok=True)
                shutil.copy(sp, keep)
                os.replace(image, os.path.join(keep, 'image.apfs'))
                args.k = True
            if not args.no_crash and rnd == 0 and not problems:
                failures += crash_test(work, base, sp, script, snaps)
    finally:
        if args.k:
            print('work directory kept: ' + work)
        else:
            shutil.rmtree(work, ignore_errors=True)
    print('%d round(s) failed' % failures if failures else 'all rounds passed')
    return 1 if failures else 0


def crash_test(work, base, sp, script, snaps):
    """Cut the power after every n-th device write: the image must be the model at a commit."""
    image = os.path.join(work, 'crash.apfs')
    total = None
    # how many device writes a full run makes: count by failing at an absurd number
    for n in range(0, 100000):
        clone(base, image)
        r = subprocess.run([TOOL, '--fail-after', str(n), 'do', image, sp], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
        out = r.stdout.decode('utf-8', 'replace')
        done = sum(1 for ln in out.splitlines() if (' commit ok' in ln and ln[:1].isdigit()) or
                   ln == 'end commit ok')
        ck, ckout = apfsck(image, crashed=True)
        state = image_state(image) if ck == 0 else None
        want, loose = snaps[min(done, len(snaps) - 1)]
        nxt, nloose = snaps[min(done + 1, len(snaps) - 1)]
        if ck != 0 or (diff(want, state, loose) and diff(nxt, state, nloose)):
            print('  [FAIL] power cut after %d writes (%d commits through): %s' % (
                n, done, ckout.strip()[-300:] if ck else '; '.join(diff(want, state, loose)[:3])))
            return 1
        if r.returncode == 0:
            total = n
            break
    print('  [ok] power cut after each of the %d device writes: always a committed state' % total)
    return 0


if __name__ == '__main__':
    sys.exit(main())
