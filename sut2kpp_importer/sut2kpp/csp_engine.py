# CspBrush -> preset for Krita's "CSP Brush" engine (paintop id cspbrush, plugins/paintops/cspbrush).

# Unlike mapping.py, nothing is translated into Krita concepts: every Variant column is copied to a
# "CSP/<column>" parameter and the engine implements CSP's behaviour. Binary columns are re-encoded as text:
#   *Effector (dynamics)     "on=p,v;p=0.3@0,0 0.68,1,c 1,1;t=1;v=0.92;r=0.85;tmax=160": CSP's effect source popup,
#                            minimums as fractions (unclamped, random may be negative), raw CSP curves with the points
#                            CSP repeats written once as corners (,c); see CspEffector.h
#   BrushPatternImageArray   CSP/Tips/Count, CSP/Tips/<i>/Name, CSP/Tips/<i>/Png (base64 RGBA PNG); the same PNGs go
#                            into the .bundle as Krita brush tips (brushes/<name>_tip<N>.png), not linked
#   TextureImage             see below; DualPatternImageArray, DualTextureImage: dropped (no dual brush)
#   BrushInOutTarget         "1001:1:30;1021:0:0"  (target:enabled:minimum percent; ignored, see IGNORED)
#   other blobs              "b64:<base64>"
# The texture image becomes a Krita library pattern: it goes into the .bundle, and the preset names it
# (Texture/Pattern/PatternMD5Sum, PatternFileName, Name) with an inline copy as a fallback. The engine composites it with CSP's own CSP/Texture* settings.
# Engine-only settings use an "x-" prefix (x-ToolPressureGraph, x-VelocityFast, x-VelocityKnee).

import base64
import struct

from . import csp_model
from . import dynamics as dyn
from . import tips as tipmod
from .kpp_writer import Preset
from . import mapping
from .mapping import Conversion, Report

PAINTOP = "cspbrush"
SKIP_COLUMNS = {"_PW_ID", "VariantID", "VariantShowSeparator", "VariantShowParam"}
INPUT_KEYS = {"pressure": "p", "tilt": "t", "velocity": "v", "random": "r"}
# the texture is a Krita pattern and the dual brush (tips, texture) is dropped. TODO: Dual brush (use masked engine?)
TIP_COLUMNS = {"BrushPatternImageArray": "Tips", "DualPatternImageArray": None,
               "TextureImage": None, "DualTextureImage": None}

# What the engine paints today. reported here so a converted preset says what to expect
NOT_YET = [("BrushUseWaterColor", "colour mixing"), ("BrushUseWaterEdge", "watercolor edge")]
# Painted, with behaviour still to be fitted to CSP reference strokes
UNCALIBRATED = [("BrushUseSpray", "spray bias / particle direction"),
                ("BrushRibbon", "ribbon orientation and corners")]
NEEDS_CORE_HOOK = [("Stickness", "taper (선 꼬리 효과)"),
                   ("BrushWaterEdgeAfterDrag", "watercolor edge after drag")]
# Still copied to the preset, but the engine ignores them. Starting and ending was dropped from the CSP Brush
# engine (BrushUseIn, BrushUseOut, BrushInOutType, BrushIn/OutLength, BrushIn/OutRatio, BrushInOutBySpeed,
# BrushInOutTarget) b/c Krita doesn't keep track of when stroke ends. Not really high priority b/c I don't use them
IGNORED = [("BrushUseIn", "starting taper"), ("BrushUseOut", "ending taper")]
TOOL_SETTINGS = [("FlickerReduction", "stabilization"), ("BrushUseRevision", "post correction")]


def _num(v):
    s = f"{v:.6f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)
    return s if s not in ("", "-0") else "0"


def effector_text(e):
    """csp_model.Effector -> the engine's effector string (raw CSP curves, minimums as fractions)."""
    parts = ["on=" + ",".join(INPUT_KEYS[n] for n in e.enabled)]
    for name, key in INPUT_KEYS.items():
        text = f"{key}={_num(e.mins[name])}"
        pts = e.curves.get(name)
        if pts and name != "random":
            # the engine's curves are Krita splines: one point per x, CSP's repeated points become corners
            corners = dyn.repeated_x(pts)
            text += "@" + " ".join(f"{_num(x)},{_num(y)}{',c' if any(abs(x - c) < 1e-4 for c in corners) else ''}"
                                   for x, y in dyn.clean(pts))
        parts.append(text)
    if e.tilt_max != 100:
        parts.append(f"tmax={e.tilt_max}")
    return ";".join(parts)


def in_out_targets(blob):
    out = []
    if isinstance(blob, bytes) and len(blob) >= 12:
        for k in range(struct.unpack_from(">i", blob, 4)[0]):
            pid, on, minimum = struct.unpack_from(">3i", blob, 12 + 12 * k)
            out.append(f"{pid}:{on}:{minimum}")
    return ";".join(out)


def tip_params(prefix, tips, tip_max):
    p = {f"CSP/{prefix}/Count": str(len(tips))}
    for i, t in enumerate(tips):
        if tip_max:
            t = tipmod.resize(t, tip_max)
        p[f"CSP/{prefix}/{i}/Name"] = t.name
        p[f"CSP/{prefix}/{i}/Png"] = base64.b64encode(tipmod.png(t.width, t.height, t.rgba)).decode("ascii")
    return p


def tip_files(b, tips, tip_max):
    """The tips as Krita brush tips for the .bundle (brushes/), one PNG each, the same images as CSP/Tips/<i>/Png.
    The "csp_tip" text tells the CSP Brush's "Add images" to take them as they are (Krita reads an opaque gray
    PNG as a mask, which would lose a two-color tip's white part)."""
    files = {}
    for i, t in enumerate(tips):
        if tip_max:
            t = tipmod.resize(t, tip_max)
        name = f"{b.name} {i + 1}" if len(tips) > 1 else b.name
        fname = f"{mapping.resource_name(b.name)}_tip{i + 1}.png"
        files[fname] = tipmod.png(t.width, t.height, t.rgba, {"csp_tip": "1", "brush_name": name})
    return files


def convert(b, tip_max=1024):
    rep = Report()
    p = {}
    db_tips = {}

    for col, value in b.v.items():
        if col in SKIP_COLUMNS or value is None:
            continue
        if isinstance(value, bytes):
            if col.endswith("Effector"):
                e = csp_model.parse_effector(value)
                if e is not None:
                    p[f"CSP/{col}"] = effector_text(e)
                continue
            if col in TIP_COLUMNS:
                db_tips[col] = value
                continue
            if col == "BrushInOutTarget":  # ignored by the engine (Starting and ending dropped)
                p[f"CSP/{col}"] = in_out_targets(value)
                continue
            p[f"CSP/{col}"] = "b64:" + base64.b64encode(value).decode("ascii")
        else:
            p[f"CSP/{col}"] = _num(value)

    for col, prefix in TIP_COLUMNS.items():
        if col not in db_tips or prefix is None:
            continue
        if col == "BrushPatternImageArray":
            tips = b.tips
        elif col == "DualPatternImageArray":
            tips = getattr(b, "dual_tips", [])
        elif col == "TextureImage":
            tips = [b.texture] if b.texture else []
        else:
            tips = []
        if tips:
            p.update(tip_params(prefix, tips, tip_max))
            rep.add(f"{col}: {len(tips)} image(s)", f"CSP/{prefix}/*", "exact")

    preset = Preset(b.name, PAINTOP, {})
    _texture(b, preset, p, rep)
    # the tips also go into Krita's brush tip library; the preset doesn't link them
    files = tip_files(b, b.tips, tip_max) if "BrushPatternImageArray" in db_tips else {}
    for fname, data in files.items():
        preset.bundle_only.append(("brushes", fname, data))
    if b.get("UseDualBrush"):
        rep.add("Dual brush", "—", "dropped", "the CSP Brush engine has no dual brush")

    if b.pressure_graph:
        p["CSP/x-ToolPressureGraph"] = " ".join(f"{_num(x)},{_num(y)}" for x, y in b.pressure_graph)
        rep.notes.append("CSP tool pressure graph stored as CSP/x-ToolPressureGraph (not applied).")

    rep.add(f"{len([k for k in p if k.startswith('CSP/')])} CSP settings", "CSP/* parameters", "exact",
            "copied verbatim; the engine implements them")
    for key, label in NOT_YET:
        if b.get(key):
            rep.add(label, "stored", "approx", "kept in the preset; not painted by this engine version yet")
    for key, label in UNCALIBRATED:
        if b.get(key):
            rep.add(label, "painted", "approx", "first approximation; refined after the CSP reference session")
    for key, label in NEEDS_CORE_HOOK:
        if b.get(key):
            rep.add(label, "stored", "approx", "needs Krita to tell the brush when a stroke ends")
    for key, label in IGNORED:
        if b.get(key):
            rep.add(label, "stored", "dropped", "ignored by the CSP Brush engine")
    for key, label in TOOL_SETTINGS:
        if b.get(key):
            rep.add(f"{label} ({key}={b.get(key)})", "—", "dropped", "Krita tool setting, not part of a preset")

    preset.params.update(p)
    return Conversion(preset, rep, files)


def _texture(b, preset, p, rep):
    """The texture image as a Krita library pattern (bundled, named by the preset, inline as a fallback)."""
    pattern = mapping.texture_pattern(b)
    if not pattern:
        p["Texture/Pattern/Enabled"] = "false"
        return
    fname, data, keys = pattern
    p.update(keys)
    preset.bundle_only.append(("patterns", fname, data))
    mode = b.get("TextureCompositeMode", 0)
    name = mapping.TEXTURE_MODE_NAMES[mode] if 0 <= mode < len(mapping.TEXTURE_MODE_NAMES) else mode
    rep.add(f"Texture {b.texture.width}×{b.texture.height}, mode {name}", f"pattern {fname} + CSP/Texture*", "approx",
            "CSP's texture modes are first guesses, to be fitted to reference strokes")
