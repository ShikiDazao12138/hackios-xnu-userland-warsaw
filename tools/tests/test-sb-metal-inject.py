#!/usr/bin/env python3
"""Compile and run the host tests of the SpringBoard Metal injection.

sb_metal_test_sequence.m drives sb_metal_registration_sequence.inc and
sb_metal_test_compression_preference.m drives sb_metal_compression_preference.inc, each
with host stubs and the production flags of libannibale-sbmetal-experiment.dylib
(-fno-objc-arc -Wall -Wextra -Werror). The compression case sets and then removes a
value in a preferences domain named with a fresh UUID.
"""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "userland/src/tests"
CASES = (
    ("sb_metal_test_sequence.m", "SEQUENCE_PASS "),
    ("sb_metal_test_compression_preference.m", "COMPRESSION_PREFERENCE_PASS "),
)


def main():
    with tempfile.TemporaryDirectory(prefix="sb-metal-inject-") as directory:
        for name, marker in CASES:
            binary = Path(directory) / Path(name).stem
            subprocess.run(["xcrun", "clang", "-fno-objc-arc", "-Wall", "-Wextra", "-Werror",
                            "-framework", "Foundation", str(TESTS / name), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            assert result.returncode == 0 and marker in result.stdout, result.stdout + result.stderr
            print(next(line for line in result.stdout.splitlines() if line.startswith(marker)))


if __name__ == "__main__":
    main()
