#!/usr/bin/env python3
"""Stage the native Front Camera descriptor from a mode 4 profile."""

import argparse
from pathlib import Path
import plistlib
import struct


PROFILE_HEADER = struct.Struct("<8s22I32s")
PROFILE_BYTES = 6440


def read_profile(path):
    raw = path.read_bytes()
    if len(raw) != PROFILE_BYTES:
        raise ValueError("invalid native profile length")
    fields = PROFILE_HEADER.unpack_from(raw)
    if fields[:4] != (b"RWCTAB03", len(raw), 2, 4):
        raise ValueError("unsupported native front capture profile")
    width, height, stride, bayer = fields[4:8]
    if (width, height, stride) != (2592, 1458, 3248) or bayer not in range(4):
        raise ValueError("unsupported native RAW10 layout")
    if fields[9:13] != (4, 31, 0, 0x2B):
        raise ValueError("unsupported native CSI mode")
    if PROFILE_HEADER.size + 12 * sum(fields[17:23]) != len(raw):
        raise ValueError("invalid native profile table lengths")
    if not 0 < fields[16] <= 30003:
        raise ValueError("unsupported native mode frame rate")
    return width, height, fields[16] / 1000.0


def stage(base_path, profile_path, output_path):
    width, height, fps = read_profile(profile_path)
    config = plistlib.loads(base_path.read_bytes())
    devices = config["AVCaptureDevices"]
    if any(device.get("uniqueName") == "Front Camera" for device in devices):
        raise ValueError("base resource already contains a Front Camera descriptor")

    native_name = f"{width}x{height}"
    formats = {}
    for name, w, h in ((native_name, width, height), ("1080p30", 1920, 1080), ("720p30", 1280, 720)):
        formats[name] = {"Width": w, "Height": h, "PixelFormatType": "420f",
                         "CaptureStreamFormat": "Photo", "MaxFrameRateOverride": fps}
    presets = {
        "Common": {"VideoCodec": "avc1", "MinFrameRate": fps, "MaxFrameRate": fps},
        "AVCaptureSessionPresetPhoto": {"DeviceFormat": native_name},
        "AVCaptureSessionPreset1920x1080": {"DeviceFormat": "1080p30"},
        "AVCaptureSessionPreset1280x720": {"DeviceFormat": "720p30"},
        "AVCaptureSessionPresetHigh": "AVCaptureSessionPreset1920x1080",
        "AVCaptureSessionPresetMedium": "AVCaptureSessionPreset1280x720",
        "AVCaptureSessionPresetLow": "AVCaptureSessionPreset1280x720",
    }
    # CMCapture derives type 2/position 2 from the canonical physical name.
    front = {"uniqueName": "Front Camera", "mediaType": "vide", "MultiStream": {
        "FigCaptureStreamFormats": {"Photo": {
            "SensorWidth": width, "SensorHeight": height,
            # Match native_format()'s FrontEndScalerOutput geometry.
            "FES": True, "FESWidth": width, "FESHeight": height, "MaxFrameRate": fps,
        }},
        "AVCaptureDeviceFormats": formats, "AVCaptureSessionPresets": presets,
    }}
    index = next((i for i, device in enumerate(devices)
                  if device.get("uniqueName", "").startswith("Front ")), len(devices))
    devices.insert(index, front)
    # sorted keys, as every property list of the root image: tools/release/localfiles.py makes this
    # file again from iOS's and has to arrive at the same bytes
    data = plistlib.dumps(config, fmt=plistlib.FMT_BINARY)
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    temporary = output_path.with_suffix(".plist.tmp")
    temporary.write_bytes(data)
    temporary.chmod(0o644)
    temporary.replace(output_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        stage(args.base, args.profile, args.output)
    except (OSError, ValueError, KeyError, plistlib.InvalidFileException) as error:
        parser.exit(1, f"mkcameraresources.py: {error}\n")


if __name__ == "__main__":
    main()
