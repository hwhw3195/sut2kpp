# python -m sut2kpp <file.sut | folder> -o <outdir> [--bundle out.bundle] [--apply-tool-pressure] [--keep-tips]
#                   [--engine krita|csp]

import argparse
import sys
from pathlib import Path

from . import csp_engine, csp_model, mapping, tips as tipmod
from .bundle import write_bundle
from .report import write_report


FALLBACK_ICON = Path(__file__).resolve().parent / "assets" / "krita_tool_freehand.png"


def icon_png(b):
    """Preset icon: the CSP custom icon if there is one, else the first material tip filling 200×200 on white,
    else (circle tips) Krita's freehand tool icon."""
    if b.icon_png:
        return b.icon_png
    if b.get("BrushUsePatternImage") and b.tips:
        t = tipmod.fit(b.tips[0], 180)
        t = tipmod.pad(t, 200, 200)
        cov = tipmod.coverage(t)
        rgba = bytearray(len(cov) * 4)
        for i, c in enumerate(cov):
            v = 255 - c
            rgba[i * 4:i * 4 + 4] = bytes((v, v, v, 255))
        return tipmod.png(200, 200, bytes(rgba))
    return FALLBACK_ICON.read_bytes()


def convert_file(path, outdir, args):
    b = csp_model.load(path)
    if args.engine == "csp":
        conv = csp_engine.convert(b, tip_max=args.tip_max)
        if args.speed_fast:
            conv.preset.params["CSP/x-VelocityFast"] = str(args.speed_fast)
        if args.speed_knee:
            conv.preset.params["CSP/x-VelocityKnee"] = str(args.speed_knee)
    else:
        conv = mapping.convert(b, apply_tool_pressure=args.apply_tool_pressure, tip_max=args.tip_max,
                               speed_fast=args.speed_fast, speed_knee=args.speed_knee)
    stem = mapping._safe(b.name)
    kpp = conv.preset.kpp(icon_png(b))
    (outdir / f"{stem}.kpp").write_bytes(kpp)
    write_report(outdir / f"{stem}.report.md", b, conv, Path(path).name)
    if args.keep_tips:
        for name, data in conv.files.items():
            (outdir / name).write_bytes(data)
    print(f"{Path(path).name} -> {stem}.kpp ({conv.preset.paintop}, {len(kpp) // 1024} KB)")
    # bundle entries use ASCII names; the preset's display name comes from its XML and keeps the CSP name
    return ([("paintoppresets", f"{mapping.resource_name(b.name)}.kpp", kpp)] + list(conv.preset.resources)
            + list(conv.preset.bundle_only))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sut2kpp", description="Convert Clip Studio Paint .sut brushes to Krita .kpp")
    ap.add_argument("inputs", nargs="+", help=".sut files or folders")
    ap.add_argument("-o", "--outdir", default="out")
    ap.add_argument("--bundle", help="also write a Krita .bundle with all converted presets")
    ap.add_argument("--apply-tool-pressure", action="store_true",
                    help="compose CSP's tool pressure graph (global pen-pressure adjustment) into pressure curves")
    ap.add_argument("--keep-tips", action="store_true", help="also write the .png/.gih tip files")
    ap.add_argument("--tip-max", type=int, default=1024, help="longest tip side in px (0 = no limit)")
    ap.add_argument("--speed-fast", type=float, help="screen px/ms that counts as fully fast (default 2)")
    ap.add_argument("--speed-knee", type=float, help="px/ms where the log speed response bends (default 0.3)")
    ap.add_argument("--engine", choices=("krita", "csp"), default="krita",
                    help='"krita": Pixel/Color Smudge presets (default); "csp": presets for the CSP Brush engine')
    ap.add_argument("--tag",default="imported", help='Krita tag for the presets in the bundle (default "imported")')
    args = ap.parse_args(argv)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in args.inputs:
        p = Path(i)
        paths += sorted(p.glob("*.sut")) if p.is_dir() else [p]
    presets, failed = [], 0
    for path in paths:
        try:
            presets += convert_file(path, outdir, args)
        except Exception as e:                      # keep going through a folder of brushes
            failed += 1
            print(f"{path.name}: FAILED: {e}", file=sys.stderr)
    if args.bundle and presets:
        write_bundle(Path(args.bundle), presets, tags=[args.tag] if args.tag else [])
        print(f"bundle -> {args.bundle}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
