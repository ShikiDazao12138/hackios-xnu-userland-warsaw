#!/usr/bin/env python3
"""Stage iOS's CMCapture.framework/V57 with the ISP-dependent rendering of its formats turned off.

init.c mounts the result in place of iOS's V57 directory. Camera formats that claim
SmartStyleRenderingSupported or SemanticRenderingSupported make CMCapture ask the capture stream's
Metadata output for semantic masks, which the ISP of an iPhone makes and this phone's capture service
does not; the capture graph then fails to build. Both keys are set to false, every other file and value
is iOS's.
"""

import argparse
from pathlib import Path
import plistlib
import shutil

ISP_RENDERING = ("SmartStyleRenderingSupported", "SemanticRenderingSupported")


def without_isp_rendering(value):
    if isinstance(value, dict):
        return {k: False if k in ISP_RENDERING else without_isp_rendering(v) for k, v in value.items()}
    if isinstance(value, list):
        return [without_isp_rendering(v) for v in value]
    return value


def stage(source, output):
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, mode=0o755)
    for path in sorted(source.iterdir()):
        data = path.read_bytes()
        if path.suffix == ".plist":
            value = plistlib.loads(data)
            wanted = without_isp_rendering(value)
            if wanted != value:
                # sorted keys, as every property list of the root image: tools/release/localfiles.py
                # makes this file again from iOS's and has to arrive at the same bytes
                data = plistlib.dumps(wanted, fmt=plistlib.FMT_BINARY)
        target = output / path.name
        target.write_bytes(data)
        target.chmod(0o644)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="iOS's CMCapture.framework/V57")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        stage(args.source, args.output)
    except (OSError, plistlib.InvalidFileException) as error:
        parser.exit(1, f"mkcameraresources.py: {error}\n")


if __name__ == "__main__":
    main()
