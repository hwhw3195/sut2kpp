# python -m sut2kpp.calibrate -o out/  ->  out/sut2kpp_calibration.bundle

# Presets with an arrow tip (pointing right at angle 0) that settle the rotation constants in mapping.py.
# Draw a straight horizontal line, left to right, with each one:
#   R0 Rotation off                          reference direction
#   R1 Rotation on, fuzzy, strength 0        same as R0 -> ROTATION_OPTION_OFFSET = 0; flipped -> math.pi
#   R2 Drawing angle only                    arrow along the line -> LINE_DIRECTION_OFFSET = 0; across -> pi/2
#   R3 fuzzy, strength 0.1667                arrows within ±30° of R1's direction
#   R4 tilt direction + curve ±30° jitter    follows the pen azimuth, ±30° around it
# C* presets apply the converter's correction constants and should all match R0 / the pen:
#   C1 fuzzy strength 0, corrected           points right, like R0
#   C3 fuzzy 30deg, corrected                right ±30°
#   C4 tilt direction, corrected, no jitter  points the way the pen leans (along the pen body)
# S* presets: 30 px round brush whose size follows speed like a CSP "velocity, minimum 40%" setting
# (100% slow -> 40% fast), with "fully fast" at 2, 4 or 8 screen px/ms. Draw one slow-to-fast stroke with each
# and pick the one closest to CSP; pass that number as --speed-fast.

import argparse
import math
import sys
from pathlib import Path

from . import dynamics as dyn
from . import tips as tipmod
from .bundle import write_bundle
from .dynamics import Sensor
from .csp_model import Effector
from .kpp_writer import Preset
from .mapping import ROTATION_OPTION_OFFSET, TILT_DIRECTION_OFFSET


def arrow(size=64):
    """Arrow pointing right (+x), as black-on-alpha RGBA."""
    rgba = bytearray(size * size * 4)
    c = size // 2
    for y in range(size):
        for x in range(size):
            shaft = size * 0.15 <= x <= size * 0.6 and abs(y - c) <= size * 0.08
            head = x > size * 0.6 and abs(y - c) <= (size * 0.9 - x) * 0.9
            if shaft or head:
                rgba[(y * size + x) * 4 + 3] = 255
    return tipmod.png(size, size, bytes(rgba))


def preset(name, png, rotation_params, angle=0.0):
    p = Preset(name, "paintbrush")
    md5 = p.embed("brushes", "sut2kpp_arrow.png", png)
    p.params.update(dyn.option_params("Size", [], enabled=True))
    p.params.update(dyn.option_params("Opacity", [], enabled=True))
    p.params.update(dyn.option_params("Flow", [], enabled=True))
    p.params.update(rotation_params)
    p.params["brush_definition"] = (f'<Brush type="png_brush" filename="sut2kpp_arrow.png" md5sum="{md5}" scale="0.6" '
                                    f'brushApplication="0" ColorAsMask="1" spacing="1.5" useAutoSpacing="0" '
                                    f'autoSpacingCoeff="1" angle="{dyn._num(angle % (2 * math.pi))}" '
                                    f'BrushVersion="2"/>')
    p.params["requiredBrushFile"] = p.params["requiredBrushFilesList"] = "sut2kpp_arrow.png"
    return p


def speed_preset(fast):
    p = Preset(f"sut2kpp S speed fast {fast:g} px-ms", "paintbrush")
    size = dyn.effector_sensors(Effector(["velocity"], {"velocity": 0.4}, {}), speed=(fast, None))
    p.params.update(dyn.option_params("Size", size, enabled=True))
    p.params.update(dyn.option_params("Opacity", [], enabled=True))
    p.params.update(dyn.option_params("Flow", [], enabled=True))
    p.params.update(dyn.option_params("Rotation", [], enabled=False))
    p.params["brush_definition"] = ('<Brush type="auto_brush" density="1" randomness="0" spacing="0.1" '
                                    'useAutoSpacing="0" autoSpacingCoeff="1" angle="0" BrushVersion="2">'
                                    '<MaskGenerator id="default" type="circle" diameter="30" ratio="1" hfade="1" '
                                    'vfade="1" spikes="2" antialiasEdges="1"/></Brush>')
    return p


def presets():
    png = arrow()
    drawing = Sensor("drawingangle", dyn.LINEAR, {"fanCornersEnabled": "0", "fanCornersStep": "30",
                                                  "angleOffset": "0", "lockedAngleMode": "0"})
    return png, [
        preset("sut2kpp R0 rotation off", png, dyn.option_params("Rotation", [], enabled=False)),
        preset("sut2kpp R1 fuzzy strength 0", png,
               dyn.option_params("Rotation", [Sensor("fuzzy", dyn.LINEAR)], value=0.0, enabled=True)),
        preset("sut2kpp R2 drawing angle", png, dyn.option_params("Rotation", [drawing], value=1.0, enabled=True)),
        preset("sut2kpp R3 fuzzy 30deg", png,
               dyn.option_params("Rotation", [Sensor("fuzzy", dyn.LINEAR)], value=30 / 180, enabled=True)),
        preset("sut2kpp R4 tilt direction + 30deg", png,
               dyn.option_params("Rotation", [Sensor("ascension", dyn.LINEAR),
                                              Sensor("fuzzy", [(0, 0.5 - 30 / 360), (1, 0.5 + 30 / 360)])],
                                 value=1.0, enabled=True)),
        preset("sut2kpp C1 fuzzy strength 0 corrected", png,
               dyn.option_params("Rotation", [Sensor("fuzzy", dyn.LINEAR)], value=0.0, enabled=True),
               ROTATION_OPTION_OFFSET),
        preset("sut2kpp C3 fuzzy 30deg corrected", png,
               dyn.option_params("Rotation", [Sensor("fuzzy", dyn.LINEAR)], value=30 / 180, enabled=True),
               ROTATION_OPTION_OFFSET),
        preset("sut2kpp C4 tilt direction corrected", png,
               dyn.option_params("Rotation", [Sensor("ascension", dyn.LINEAR)], value=1.0, enabled=True),
               ROTATION_OPTION_OFFSET + TILT_DIRECTION_OFFSET),
    ] + [speed_preset(f) for f in (2, 4, 8)]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sut2kpp.calibrate")
    ap.add_argument("-o", "--outdir", default="out")
    args = ap.parse_args(argv)
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    png, ps = presets()
    entries = [("brushes", "sut2kpp_arrow.png", png)]
    for p in ps:
        icon = png if " S " not in p.name else (Path(__file__).parent / "assets/krita_tool_freehand.png").read_bytes()
        entries.append(("paintoppresets", p.name.replace(" ", "_") + ".kpp", p.kpp(icon)))
    path = out / "sut2kpp_calibration.bundle"
    write_bundle(path, entries, "sut2kpp calibration", tags=["sut2kpp calibration"])
    print(f"{len(ps)} presets -> {path}")


if __name__ == "__main__":
    sys.exit(main())
