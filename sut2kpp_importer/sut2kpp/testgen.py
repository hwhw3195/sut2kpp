# Generate batches of CSP test brushes from template .sut files, for reverse-engineering CSP's brush engine.
#
#   python -X utf8 -m sut2kpp.testgen spec.json -o testbrushes
#
# spec.json:
# {
#   "prefix": "S1",
#   "tests": [
#     {"name": "gap-fixed-{BrushInterval}", "template": "example_brush/SOIPEN.sut",
#      "no_dynamics": true, "set": {"BrushAutoIntervalType": 0}, "grid": {"BrushInterval": [5, 25, 100]}}
#   ]
# }
# Each test expands its "grid" (cartesian product) and writes one .sut per combination named
# "<prefix>-<NNN> <name>" (CSP lists sub tools by name, so the numbering keeps the sweep order).
# "set_all" in the spec applies to every test (before the test's own "set").
# Template paths are relative to the spec file. Every Variant row is updated (current and initial settings),
# NodeName is replaced and NodeUuid regenerated so CSP imports each file as a separate sub tool.
# "no_dynamics": true switches every dynamics popup off (mouse strokes then paint with constant values).

import argparse
import itertools
import json
import shutil
import sqlite3
import struct
import sys
import uuid
from pathlib import Path


def expand(test, set_all=None):
    grid = test.get("grid", {})
    keys = list(grid)
    for values in itertools.product(*(grid[k] for k in keys)) if keys else [()]:
        settings = dict(set_all or {})
        settings.update(test.get("set", {}))
        settings.update(zip(keys, values))
        yield test["name"].format(**settings), settings


def no_dynamics(blob):
    """Clear the enabled-input mask of an effector blob (header: [len][available][enabled]...)."""
    if not isinstance(blob, bytes) or len(blob) < 12 or struct.unpack_from(">i", blob, 0)[0] not in (44, 48):
        return blob
    return blob[:8] + struct.pack(">i", 0) + blob[12:]


def write_variant(src, dst, name, settings, clear_dynamics):
    shutil.copyfile(src, dst)
    db = sqlite3.connect(dst)
    try:
        cols = {r[1] for r in db.execute("pragma table_info(Variant)")}
        unknown = [k for k in settings if k not in cols]
        if unknown:
            raise ValueError(f"unknown Variant column(s): {', '.join(unknown)}")
        for key, value in settings.items():
            db.execute(f'update Variant set "{key}"=?', (value,))
        if clear_dynamics:
            effectors = [c for c in cols if c.endswith("Effector")]
            for vid, *blobs in db.execute(f'select VariantID, {", ".join(chr(34) + c + chr(34) for c in effectors)} '
                                          f'from Variant').fetchall():
                for col, blob in zip(effectors, blobs):
                    if isinstance(blob, bytes):
                        db.execute(f'update Variant set "{col}"=? where VariantID=?', (no_dynamics(blob), vid))
        db.execute("update Node set NodeName=?, NodeUuid=?", (name, uuid.uuid4().bytes))
        db.commit()
    finally:
        db.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sut2kpp.testgen", description="Generate CSP test brushes from a spec")
    ap.add_argument("spec")
    ap.add_argument("-o", "--outdir", default="testbrushes")
    args = ap.parse_args(argv)
    spec_path = Path(args.spec)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    prefix = spec.get("prefix", "T")
    n = 0
    index = []
    for test in spec["tests"]:
        template = (spec_path.parent / test["template"]).resolve()
        for name, settings in expand(test, spec.get("set_all")):
            n += 1
            full = f"{prefix}-{n:03d} {name}"
            write_variant(template, out / f"{full}.sut", full, settings, test.get("no_dynamics", False))
            index.append({"name": full, "template": test["template"], "set": settings,
                          "no_dynamics": test.get("no_dynamics", False), "paths": test.get("paths", "all"),
                          "canvas": test.get("canvas", "white")})
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{n} test brushes -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
