#!/usr/bin/env python3
"""apfs-tool annfs (cmd_annfs.c) on a made-up ANNFS001 partition, checked object by object.

Each round writes a partition image the way bsd/miscfs/ramfs/ramfs_disk.inc lays it out: two
metadata slots (the newer one is the tree, the older one something else), data pages of 16 KiB
at 512 MiB + 16 KiB * n, holes among them, some in the last 32 MiB the container leaves out.  The tree has directories, regular files of every
size, hard links, symbolic links, FIFOs and character devices, small extended attributes and
ones too big for the record, BSD flags, protection classes, document ids, sync-root marks,
inode numbers below 16 and an object no directory names.  Then, as annfs-to-apfs.sh does:
mkapfs.py makes an empty container for the partition less its last 32 MiB, apfs-tool annfs
converts (given those 32 MiB as well), and only the image's first 512 MiB is put over the
partition.  The result must pass
apfsck -cw, and apfs.py must read back every object as it was: type, mode, owner, times, flags,
class, document id, inode number (16 and up), link count, attributes, link target, contents.

usage: test_annfs.py [--rounds N] [--seed S] [--objects N] [-k]
"""
import argparse
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import unicodedata
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import apfs                                                     # noqa: E402

TOOL = os.path.join(ROOT, 'apfs-work', 'apfs-tool')
APFSCK = os.path.join(ROOT, 'xnu-deps', 'build', 'apfsprogs', 'apfsck', 'apfsck')
MIB = 1 << 20
PAGE = 16384
PART = 1024 * MIB                       # the partition
TAIL = 32 * MIB                         # left out of the container (darwin_nvram)
HEAD = 512 * MIB                        # what the conversion writes
VREG, VDIR, VCHR, VLNK, VFIFO = 1, 2, 4, 5, 7
KIND = {VREG: apfs.S_IFREG, VDIR: apfs.S_IFDIR, VCHR: apfs.S_IFCHR, VLNK: apfs.S_IFLNK, VFIFO: apfs.S_IFIFO}
INO_EXT_TYPE_DOCUMENT_ID = 3
INODE_IS_SYNC_ROOT = 0x00200000


def a8(n):
    return (n + 7) & ~7


class Obj:
    def __init__(self, ino, vtype, r):
        self.ino, self.type = ino, vtype
        self.mode = r.choice([0o755, 0o644, 0o600, 0o700, 0o4755])
        self.uid, self.gid = r.choice([(0, 0), (501, 501), (33, 20)])
        # UF_TRACKED, UF_HIDDEN, UF_COMPRESSED (no decmpfs attribute here: taken off)
        self.flags = r.choice([0, 0, 0x40, 0x8000, 0x20])
        self.pclass = r.choice([0, 3, 4, 7])                        # 7: CX
        self.iflags = 1 if vtype == VDIR and r.random() < 0.2 else 0
        # ids below 3 are APFS's own and get new ones
        self.docid = r.choice([1, 2, r.randrange(3, 1 << 20)]) if r.random() < 0.2 else 0
        self.times = [(r.randrange(1500000000, 1800000000), r.randrange(10 ** 9)) for _ in range(4)]
        self.entries = []                                           # (name, child)
        self.target = b''
        self.data = b''
        self.pages = []                                             # dblk per page, 0: a hole
        self.xattrs = {}
        self.nlink = 0


def make_tree(r, count):
    """The objects, the root (inode 2) first; a file's pages are numbered here and written by write_partition."""
    inos = iter(r.sample(range(3, 40 * count), 3 * count))
    objs, dirs = [], []
    root = Obj(2, VDIR, r)
    objs.append(root)
    dirs.append(root)
    used = set()
    page_end = (PART - HEAD) // PAGE
    tail_page = (PART - TAIL - HEAD) // PAGE

    def new_page():
        # runs of neighbours as well as scattered ones
        if used and r.random() < 0.6 and max(used) + 1 < page_end:
            p = max(used) + 1
        elif r.random() < 0.05:
            p = r.randrange(tail_page - 2, page_end)                # across the container's end, and past it
        else:
            p = r.randrange(1, page_end)
        while p in used:
            p = r.randrange(1, page_end)
        used.add(p)
        return p

    def name():
        base = r.choice(['a', 'Library', 'with space', 'café', '日本', 'x.plist', 'n'])
        return unicodedata.normalize('NFC', '%s %d' % (base, r.randrange(10 ** 6))).encode()

    for _ in range(count):
        d = r.choice(dirs)
        kind = r.choice([VREG] * 6 + [VDIR] * 3 + [VLNK, VFIFO, VCHR])
        o = Obj(next(inos), kind, r)
        o.nlink = 1
        if kind == VDIR:
            dirs.append(o)
        elif kind == VLNK:
            o.target = r.choice([b'../a', b'/private/var/mobile', b'x' * r.randrange(1, 900)])
        elif kind == VREG:
            size = r.choice([0, 1, 4095, 4096, 4097, PAGE, PAGE + 1, r.randrange(1, 300000 if count <= 1000 else 30000)])
            o.data = bytearray(r.getrandbits(8 * size).to_bytes(size, 'little')) if size else bytearray()
            for pg in range((size + PAGE - 1) // PAGE):
                if r.random() < 0.15:
                    o.pages.append(0)
                    o.data[pg * PAGE:(pg + 1) * PAGE] = bytes(min(PAGE, size - pg * PAGE))
                else:
                    o.pages.append(new_page())
            o.pages += [0] * r.choice([0, 0, 3])                    # a block table longer than the file
        for _ in range(r.choice([0, 0, 1, 3] if count <= 1000 else [0, 0, 0, 1])):
            n = 'com.example.%d' % r.randrange(1000)
            o.xattrs[n] = r.getrandbits(8 * 64).to_bytes(64, 'little') * r.choice([0, 1, 10, 60, 300])
        d.entries.append((name(), o))
        objs.append(o)
    files = [o for o in objs if o.type != VDIR]
    for _ in range(count // 10):                                    # hard links
        o = r.choice(files)
        r.choice(dirs).entries.append((name(), o))
        o.nlink += 1
    for o in r.sample(objs[1:], 3):                                 # numbers APFS keeps for itself
        o.ino = r.choice([3, 5, 7, 9, 11, 15])
        if o.ino in {x.ino for x in objs if x is not o}:
            o.ino = next(inos)
    objs.append(Obj(next(inos), VREG, r))                           # named by no directory
    objs[-1].nlink = 1
    return objs


def payload(objs, r):
    out = bytearray()
    for o in r.sample(objs, len(objs)):                             # in no particular order
        size = len(o.target) if o.type == VLNK else len(o.data)
        ts = [v for t in o.times for v in t]
        out += struct.pack('<QQQ14I8q', o.ino, size, 2, o.type, o.mode, o.uid, o.gid, o.flags, o.pclass,
                           o.nlink if o.type != VDIR else 2, len(o.target), len(o.pages), len(o.xattrs),
                           len(o.entries), o.iflags, o.docid, 0, *ts)
        out += o.target + bytes(a8(len(o.target)) - len(o.target))
        tbl = struct.pack('<%dI' % len(o.pages), *o.pages)
        out += tbl + bytes(a8(len(tbl)) - len(tbl))
        for n, v in o.xattrs.items():
            nb = n.encode()
            out += struct.pack('<II', len(nb), len(v)) + nb + v + bytes(a8(len(nb) + len(v)) - len(nb) - len(v))
        for n, c in o.entries:
            out += struct.pack('<QII', c.ino, len(n), 0) + n + bytes(a8(len(n)) - len(n))
    return bytes(out)


def slot(gen, nodes, pay):
    sup = bytearray(4096)
    struct.pack_into('<8sIIQQQII', sup, 0, b'ANNFS001', 3, PAGE, gen, len(pay), 5000, nodes,
                     zlib.crc32(pay))
    struct.pack_into('<I', sup, 48, zlib.crc32(bytes(sup[:48])))
    return bytes(sup) + pay


def write_partition(path, objs, r):
    with open(path, 'wb') as f:
        f.truncate(PART)
        slots = [(7, objs), (6, objs[:1])]                          # the older one: only the root
        r.shuffle(slots)
        for off, (gen, which) in zip((0, 32 * MIB), slots):
            f.seek(off)
            f.write(slot(gen, len(which), payload(which, r)))
        for o in objs:
            for pg, dblk in enumerate(o.pages):
                if dblk:
                    chunk = bytes(o.data[pg * PAGE:(pg + 1) * PAGE])
                    f.seek(HEAD + dblk * PAGE)
                    # what follows the file's end in its last page is not the file's
                    f.write(chunk + bytes(r.getrandbits(8) | 1 for _ in range(min(64, PAGE - len(chunk)))))


def check(image, objs):
    c = apfs.Container(image)
    v = apfs.pick_volume(c, None)
    want = {}

    def collect(d, path):
        for n, o in d.entries:
            p = path + '/' + n.decode()
            want[p] = o
            if o.type == VDIR:
                collect(o, p)
    collect(objs[0], '')
    problems = []
    seen = set()
    got_paths = set()
    for p, name, fid, dt in apfs.walk(v, apfs.ROOT_DIR_INO_NUM):
        got_paths.add(p)
        o = want.get(p)
        if o is None:
            problems.append('%s: not in the tree' % p)
            continue
        problems += compare(v, p, fid, o, seen)
    for p in set(want) - got_paths:
        problems.append('%s: missing' % p)
    problems += compare(v, '/', apfs.ROOT_DIR_INO_NUM, objs[0], seen)
    docs = [d for d in seen_docids(v, seen)]
    if len(docs) != len(set(docs)):
        problems.append('document ids used twice')
    # nodes filled front to back left them nearly empty (4.6 records each) and the tree 8 deep
    t = v.tree
    if t.node_count >= 10 and t.key_count / t.node_count < 10:
        problems.append('file-system tree: %.1f records a node' % (t.key_count / t.node_count))
    c.close()
    return problems


def seen_docids(v, ids):
    for fid in ids:
        d = v.inode(fid).xf.get(INO_EXT_TYPE_DOCUMENT_ID)
        if d:
            yield struct.unpack('<I', d)[0]


def compare(v, p, fid, o, seen):
    ino = v.inode(fid)
    out = []

    def same(what, a, b):
        if a != b:
            out.append('%s: %s %r, want %r' % (p, what, a, b))
    same('type', ino.ifmt, KIND[o.type])
    same('mode', ino.mode & 0o7777, o.mode)
    same('owner', (ino.owner, ino.group), (o.uid, o.gid))
    same('flags', ino.bsd_flags, o.flags & ~0x20)
    same('class', ino.protection_class, o.pclass)
    same('times', (ino.access_time, ino.mod_time, ino.change_time, ino.create_time),
         tuple(s * 10 ** 9 + n for s, n in o.times))
    same('sync root', bool(ino.internal_flags & INODE_IS_SYNC_ROOT), bool(o.iflags))
    d = ino.xf.get(INO_EXT_TYPE_DOCUMENT_ID)
    docid = struct.unpack('<I', d)[0] if d else 0
    if o.docid >= 3 or not o.docid:
        same('document id', docid, o.docid)
    elif docid < 3:
        out.append('%s: document id %d, want a new one' % (p, docid))
    if o.ino >= 16 or o.ino == 2:
        same('inode', fid, o.ino)
    if o.type != VDIR:
        same('links', ino.nlink, o.nlink)
    if fid in seen:
        return out
    seen.add(fid)
    xs = {n: v.xattr_bytes(fid, n, ino.xattrs) for n in ino.xattrs if not n.startswith('com.apple.fs.')}
    same('attributes', xs, o.xattrs)
    if o.type == VLNK:
        same('target', ino.symlink_target_bytes(), o.target)
    if o.type == VREG:
        got = bytes(ino.contents())
        if got != bytes(o.data):
            out.append('%s: contents differ (%d bytes, want %d)' % (p, len(got), len(o.data)))
    return out


def run_round(work, seed, count):
    r = random.Random(seed)
    objs = make_tree(r, count)
    part = os.path.join(work, 'part.img')
    write_partition(part, objs, r)
    empty = os.path.join(work, 'empty')
    os.makedirs(empty, exist_ok=True)
    image = os.path.join(work, 'apfs.img')
    subprocess.run([sys.executable, os.path.join(HERE, 'mkapfs.py'), '-o', image, '--blocks',
                    str((PART - TAIL) // 4096), 'Data=%s,role=data' % empty], check=True, stdout=subprocess.DEVNULL)
    head, tail = os.path.join(work, 'head.bin'), os.path.join(work, 'tail.bin')
    with open(part, 'rb') as f, open(head, 'wb') as h, open(tail, 'wb') as t:
        h.write(f.read(64 * MIB))
        f.seek(PART - TAIL)
        t.write(f.read(TAIL))
    res = subprocess.run([TOOL, 'annfs', image, head, tail], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = res.stdout.decode('utf-8', 'replace')
    if res.returncode != 0:
        return ['apfs-tool annfs: ' + log.strip()[-800:]]
    # what goes to the partition: the image's first 512 MiB, nothing else
    with open(image, 'rb') as f, open(part, 'r+b') as p:
        p.write(f.read(HEAD))
        f.seek(HEAD)
        for off in range(HEAD, (PART - TAIL), 64 * MIB):
            blk = f.read(64 * MIB)
            if blk.strip(b'\0'):
                return ['the conversion wrote past 512 MiB, near %d MiB' % (off // MIB)]
    problems = check(part, objs)
    ck = subprocess.run([APFSCK, '-cw', part], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if ck.returncode != 0:
        problems.append('apfsck: ' + ck.stdout.decode('utf-8', 'replace').strip()[-600:])
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--rounds', type=int, default=4)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--objects', type=int, default=140, help='objects in a tree (20000: trees of several levels)')
    ap.add_argument('-k', action='store_true', help='keep the work directory')
    args = ap.parse_args()
    work = tempfile.mkdtemp(prefix='annfs-')
    failed = 0
    for i in range(args.rounds):
        problems = run_round(work, args.seed + i, args.objects)
        print('round %d (seed %d): %s' % (i, args.seed + i, 'ok' if not problems else '%d problems' % len(problems)))
        for p in problems[:20]:
            print('  ' + p)
        failed += bool(problems)
    if args.k:
        print('work directory: ' + work)
    else:
        shutil.rmtree(work)
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
