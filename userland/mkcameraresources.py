#!/usr/bin/env python3
"""Stage iOS's CMCapture.framework/V57 with the camera features that need an iPhone's ISP turned off.

init.c mounts the result in place of iOS's V57 directory. This phone's capture service gives processed
frames only, and the capture graph fails to build when the resources claim one of these:
- SmartStyleRenderingSupported, SemanticRenderingSupported: CMCapture asks the capture stream's Metadata
  output for semantic masks, which the ISP of an iPhone makes.
- softISPSupported (the device's), ConfigureForSoftISPSupport (a format's): the source node gets a still
  image sensor raw output (the stream's StillImageRaw) for the software ISP of the photo pipeline, which
  takes compressed Bayer frames the stream does not have.
These keys are set to false, every other file and value is iOS's.
"""

import argparse
from pathlib import Path
import plistlib
import shutil

ISP_FEATURES = ("SmartStyleRenderingSupported", "SemanticRenderingSupported", "softISPSupported",
                "ConfigureForSoftISPSupport")


def without_isp_features(value):
    if isinstance(value, dict):
        return {k: False if k in ISP_FEATURES else without_isp_features(v) for k, v in value.items()}
    if isinstance(value, list):
        return [without_isp_features(v) for v in value]
    return value


def stage(source, output):
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, mode=0o755)
    for path in sorted(source.iterdir()):
        data = path.read_bytes()
        if path.suffix == ".plist":
            value = plistlib.loads(data)
            wanted = without_isp_features(value)
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
