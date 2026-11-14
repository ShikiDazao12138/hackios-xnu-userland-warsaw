#!/usr/bin/env python3
"""Exercise the production APFS writer against disposable images and apfsck."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def run(*args, data=None):
    result = subprocess.run(args, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=30)
    output = result.stdout.decode("utf-8", "replace")
    if "AddressSanitizer" in output or "runtime error:" in output:
        raise AssertionError(output)
    return result.returncode, output


def digest(path):
    return hashlib.sha256(path.read_bytes()).digest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", type=Path, default=ROOT / "apfs-work/apfs-tool")
    args = parser.parse_args()
    tool = str(args.tool.resolve())
    checker = str(ROOT / "xnu-deps/build/apfsprogs/apfsck/apfsck")
    checks = 0
    with tempfile.TemporaryDirectory(prefix="apfs-txn-safety-") as tmp:
        work = Path(tmp)
        tree = work / "tree"
        tree.mkdir()
        (tree / "small").write_bytes(b"A" * 100)
        (tree / "block").write_bytes(b"B" * 4096)
        base = work / "base.apfs"
        rc, output = run(sys.executable, str(HERE / "mkapfs.py"), "-o", str(base), "data=" + str(tree))
        assert rc == 0, output

        def fresh():
            image = work / "candidate.apfs"
            if image.exists():
                image.unlink()
            if sys.platform == "darwin":
                subprocess.run(["cp", "-c", str(base), str(image)], check=True)
            else:
                shutil.copyfile(base, image)
            return image

        def consistent(image):
            rc, output = run(checker, "-w", str(image))
            assert rc == 0, output

        for name in ["a" * 52, "b" * 120, "c" * 255]:
            image = fresh()
            before = digest(image)
            rc, output = run(tool, "create", str(image), "/" + name, "0644")
            if rc:
                assert digest(image) == before, output
            else:
                rc, output = run(tool, "stat", str(image), "/" + name)
                assert rc == 0, output
            consistent(image)
            checks += 1

        for offset, payload in [(100, b"C"), (99, b"D" * 4100), (2**64 - 1, b"E" * 2)]:
            image = fresh()
            before = digest(image)
            rc, output = run(tool, "write-file", str(image), "/small", str(offset), "-", data=payload)
            if rc:
                assert digest(image) == before, output
            else:
                rc, output = run(tool, "cat", str(image), "/small")
                assert rc == 0 and output.encode()[offset:offset + len(payload)] == payload, output
            consistent(image)
            checks += 1

        image = fresh()
        for i in range(50):
            rc, output = run(tool, "create", str(image), f"/entry-{i:03d}", "0644")
            assert rc == 0, f"creation failed at {i}: {output}"
            consistent(image)
            rc, output = run(tool, "stat", str(image), f"/entry-{i:03d}")
            assert rc == 0, f"stat failed at {i}: {output}"
        checks += 1

        # Test inserting long filenames into split tree (verifying btree_info max key size in info footer)
        for long_name in ["a_50_byte_filename_that_exceeds_previous_short_keys", "b" * 128, "c" * 255]:
            rc, output = run(tool, "create", str(image), "/" + long_name, "0644")
            assert rc == 0, f"creation of long filename in split tree failed: {output}"
            consistent(image)
            rc, output = run(tool, "stat", str(image), "/" + long_name)
            assert rc == 0, f"stat of long filename in split tree failed: {output}"
            checks += 1

        # A failed publication must leave a checkpoint the independent checker can read.
        for stop in range(18):
            image = fresh()
            rc, output = run(tool, "--fail-after", str(stop), "create", str(image), "/fault", "0644")
            consistent(image)
            checks += 1
    print(f"APFS transaction safety: {checks} checks passed")


if __name__ == "__main__":
    main()
