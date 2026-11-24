# CspBrush -> Krita preset (Pixel brush, or Color Smudge when CSP colour mixing is on) + conversion report.

import math
from dataclasses import dataclass, field

from . import dynamics as dyn
from . import tips as tipmod
from .csp_model import Tip
from .dynamics import Sensor
from .kpp_writer import Preset

# Rotation calibration constants (see tests/test_mapping.py and `python -m sut2kpp.calibrate`).
# Tip angles are counter-clockwise on screen (KisBrush renders -(rotation + angle) through a clockwise QTransform).
# Confirmed in Krita 5.2: a checked Rotation option without a Drawing angle sensor adds 180° (KisRotationOption
# returns pi * (1 - S * additive)); calibration R1 points left where R0 points right. Cancelled on the tip angle.
ROTATION_OPTION_OFFSET = math.pi
# Confirmed: Drawing angle aligns the tip's +x with the stroke direction (calibration R2).
LINE_DIRECTION_OFFSET = 0.0
# Measured with R4 (tip 0°, uncorrected): the arrow points 90° clockwise of the side the pen leans to. After the
# 180° correction it would point 90° counter-clockwise of it; -90° more makes the tip's +x point the way the pen
# leans (along the pen body, away from the nib). Whether CSP's 펜 방향 means this or the opposite (towards the nib)
# is still to be compared in CSP: if it's the opposite, add math.pi here.
TILT_DIRECTION_OFFSET = -math.pi / 2

AUTO_SPACING = {1: 0.25, 2: 0.1, 3: 0.05}           # CSP 넓음 wide / 보통 normal / 좁음 narrow
# CSP TextureCompositeMode -> Krita Texture/Pattern/TexturingMode (pixel engine numbering)
# On the Pixel brush, Normal, Multiply and Outline (and unknown modes) use Lightness Map with the flattened pattern at
# neutral point 0.5: the most predictable of the tried options (Height erased Boru Ballpen's dabs; keeping CSP
# density in the pattern alpha was less predictable). Textures remain an approximate conversion.
# This table is used for the other modes, and for Color Smudge, which has no Lightness Map texturing.
TEXTURE_MODES = {0: (0, "Normal → Multiply"), 1: (0, None), 2: (1, None), 3: (4, "Compare → Darken"),
                 4: (0, "Outline → Multiply"), 5: (5, None), 6: (6, None), 7: (10, "Hard mix → Hard Mix (Photoshop)"),
                 8: (12, None)}
LIGHTNESS_MAP = 2
TEXTURE_LIGHTNESS_MODES = {0, 1, 4}
TEXTURE_MODE_NAMES = ["Normal", "Multiply", "Subtract", "Compare", "Outline", "Overlay", "Color dodge", "Hard mix",
                      "Height"]
SPRAY_IN_ROTATION = {0x40: 0x40, 0x80: 0x80}   # spray particle bits that mean the same as tip angle bits


@dataclass
class Report:
    rows: list = field(default_factory=list)   # (csp, krita, status, note); status: exact/approx/dropped
    curves: list = field(default_factory=list)  # (option, sensor id, csp points, krita curve)
    notes: list = field(default_factory=list)

    def add(self, csp, krita, status="exact", note=""):
        self.rows.append((csp, krita, status, note))


@dataclass
class Conversion:
    preset: Preset
    report: Report
    files: dict   # filename -> bytes (tips, for --keep-tips)


def convert(b, apply_tool_pressure=False, tip_max=1024, speed_fast=None, speed_knee=None):
    paintop = "colorsmudge" if b.get("BrushUseWaterColor") else "paintbrush"
    rep = Report()
    p = {}
    files = {}
    preset = Preset(b.name, paintop)
    tool_pressure = b.pressure_graph if apply_tool_pressure else None
    if b.pressure_graph:
        rep.notes.append("CSP tool pressure graph (global pen-pressure adjustment) as a Krita curve: `"
                         + dyn.curve_str(b.pressure_graph) + "`" +
                         (" — composed into every pressure curve." if apply_tool_pressure else
                          " — not applied; paste it into Krita's Settings → Tablet curve to match CSP."))
    speed = (speed_fast, speed_knee)
    velocity_noted = False

    def sensors(setting, option):
        nonlocal velocity_noted
        eff = b.effector(setting)
        out = dyn.effector_sensors(eff, tool_pressure, speed)
        if "velocity" in eff.enabled and not velocity_noted:
            velocity_noted = True
            rep.notes.append(f"Speed curves are inverted (CSP: slow = 100%) and read on a log scale: full minimum at "
                             f"{speed_fast or dyn.VELOCITY_FAST:g} px/ms, knee {speed_knee or dyn.VELOCITY_KNEE:g} "
                             f"px/ms (Krita speed 1 = {dyn.KRITA_MAX_SPEED:g} px/ms). Tune with --speed-fast / "
                             f"--speed-knee.")
        for name, s in zip(eff.enabled, out):
            limits = f"min {eff.mins[name]:.2f}"
            if name == "tilt":
                limits += f", max {max(1.0, eff.tilt_max / 100):.2f}"
            rep.curves.append((option, s.id, eff.curves.get(name) if name != "random" else None, limits, s.curve))
        return out

    # ------------------------------------------------ size, spray
    size = b.px("BrushSize")
    spray = bool(b.get("BrushUseSpray")) and paintop == "paintbrush"
    # a tilt maximum over 100% grows the tip; the tilt curve is scaled down by the same factor
    size_sensors, size_gain = dyn.headroom(sensors("BrushSize", "Size"))
    diameter = size
    if spray:
        particle = b.px("BrushSpraySize")
        diameter = particle
        density = max(1, b.get("BrushSprayDensity", 1))
        scatter = min(5.0, (size / 2) / particle) if particle else 0
        p.update(dyn.option_params("Scatter", [], value=scatter, enabled=True))
        p["Scattering/AxisX"] = p["Scattering/AxisY"] = "true"
        rep.add(f"Spray: area {size:g} px, particle {particle:g} px, density {density}",
                f"particle tip {particle:g} px, Scatter {scatter:.3f} × diameter (both axes)", "approx",
                "Krita paints one particle per dab, so density shortens the spacing instead")
        if b.get("BrushSprayBias"):
            rep.add(f"Spray bias {b.get('BrushSprayBias')}", "—", "dropped")
        if b.get("BrushSpraySizeSyncBrushSize"):
            rep.add("Spray particle size linked to brush size", "fixed particle size", "approx")
        size_sensors = dyn.merge(size_sensors, sensors("BrushSpraySize", "Size"))
    elif b.get("BrushUseSpray"):
        rep.add("Spray effect", "—", "dropped", "not available with the Color Smudge engine")

    # ------------------------------------------------ start / end
    start = _start_taper(b, rep)
    if start.get(1001):
        size_sensors = dyn.merge(size_sensors, [start[1001]])
    p.update(dyn.option_params("Size", size_sensors, enabled=True))
    rep.add(f"Brush size {size:g} px", f"diameter {diameter:g} px", "exact")

    # ------------------------------------------------ opacity, density
    opacity_sensors = sensors("BrushOpacity", "Opacity")
    flow_sensors = sensors("BrushFlow", "Flow")
    if start.get(1021):
        flow_sensors = dyn.merge(flow_sensors, [start[1021]])
    opacity = b.get("Opacity", 100) / 100
    flow = b.get("BrushFlow", 100) / 100
    if paintop == "paintbrush":
        p.update(dyn.option_params("Opacity", opacity_sensors, value=opacity, enabled=True))
        p.update(dyn.option_params("Flow", flow_sensors, value=flow, enabled=True))
        rep.add(f"Opacity {opacity:.0%}, density {flow:.0%}", "OpacityValue, FlowValue", "exact")
    else:
        # Color Smudge has no flow: fold density (and paint density) into opacity
        mix_alpha = b.get("BrushMixAlpha", 100) / 100
        merged = dyn.merge(dyn.merge(opacity_sensors, flow_sensors), sensors("BrushMixAlpha", "Opacity"))
        p.update(dyn.option_params("Opacity", merged, value=opacity * flow * mix_alpha, enabled=True))
        rep.add(f"Opacity {opacity:.0%} × density {flow:.0%} × density of paint {mix_alpha:.0%}",
                "Opacity (Color Smudge has no flow)", "approx")

    # ------------------------------------------------ thickness
    thickness = b.get("BrushThickness", 100) / 100
    thick_sensors = sensors("BrushThickness", "Ratio")
    if thickness < 1 or thick_sensors:
        p.update(dyn.option_params("Ratio", thick_sensors, value=thickness, enabled=True))
        rep.add(f"Thickness {thickness:.0%}" + (" (vertical)" if b.get("BrushVerticalThicknes") else ""),
                "Ratio", "exact")
    else:
        p.update(dyn.option_params("Ratio", [], enabled=False))

    # ------------------------------------------------ spacing
    if b.get("BrushAutoIntervalType", 0) == 0:
        spacing = b.get("BrushInterval", 10) / 100
        rep.add(f"Spacing fixed {b.get('BrushInterval'):g}%", f"spacing {spacing:g}", "exact")
    else:
        spacing = AUTO_SPACING.get(b.get("BrushAutoIntervalType"), 0.1)
        rep.add(f"Spacing auto type {b.get('BrushAutoIntervalType')}", f"spacing {spacing:g}", "approx")
    if spray and diameter:
        spacing = spacing * size / density / diameter
        rep.add(f"Spray: {density} particles per {spacing * density * diameter:.0f} px", f"particle spacing {spacing:.3f}",
                "approx")
    spacing = max(spacing, 0.02)
    p.update(dyn.option_params("Spacing", sensors("BrushInterval", "Spacing"), enabled=None))

    # ------------------------------------------------ rotation
    rotation_on, tip_angle = _rotation(b, p, rep, spray)
    if thickness < 1 and b.get("BrushVerticalThicknes"):
        tip_angle += math.pi / 2

    # ------------------------------------------------ tip
    if size_gain > 1:
        rep.add(f"Size with the pen at its furthest lean (60°): {size_gain:.0%}",
                f"tip {diameter * size_gain:.4g} px, tilt curve / {size_gain:.3g}",
                "approx", "Krita curves stop at 100%: the brush size shown in Krita is the size with the pen flat")
    p["brush_definition"], tip_files = _brush_definition(b, preset, rep, diameter * size_gain, spacing, tip_angle,
                                                         tip_max)
    files.update(tip_files)
    if tip_files:
        p["requiredBrushFile"] = next(iter(tip_files))
        p["requiredBrushFilesList"] = ";".join(tip_files)

    # ------------------------------------------------ texture
    _texture(b, preset, p, rep, sensors, paintop)

    # ------------------------------------------------ colour
    if paintop == "colorsmudge":
        _color_smudge(b, p, rep, sensors)
    else:
        _color_jitter(b, p, rep)

    # ------------------------------------------------ blend mode & the rest
    mode = b.get("CompositeMode", 0)
    p["CompositeOp"] = "normal"
    if mode:
        rep.add(f"Blending mode code {mode}", "normal", "approx", "CSP blend codes are not mapped yet")
    _dual_brush(b, p, rep)
    for key, label in (("BrushUseWaterEdge", "Watercolor edge"), ("FlickerReduction", "Stabilization"),
                       ("BrushUseRevision", "Post correction"), ("Stickness", "Taper (선 꼬리 효과)"),
                       ("BrushUseVectorEraser", "Vector eraser"), ("BrushRibbon", "Ribbon")):
        if b.get(key):
            rep.add(f"{label} ({key}={b.get(key)})", "—", "dropped",
                    "Krita tool setting, not part of a preset" if key in ("FlickerReduction", "BrushUseRevision")
                    else "no Krita equivalent")
    for note in dict.fromkeys(b.unit_notes):
        rep.notes.append(note)

    preset.params.update(p)
    return Conversion(preset, rep, files)


# ---------------------------------------------------------------- pieces

def _start_taper(b, rep):
    """Start of stroke (시작점) by length -> Krita Distance sensor per target. Returns {target id: Sensor}."""
    targets = {}
    blob = b.v.get("BrushInOutTarget")
    if isinstance(blob, bytes) and len(blob) >= 12:
        import struct
        for k in range(struct.unpack_from(">i", blob, 4)[0]):
            pid, on, minimum = struct.unpack_from(">3i", blob, 12 + 12 * k)
            if on:
                targets[pid] = minimum / 100
    if not targets or not (b.get("BrushUseIn") or b.get("BrushUseOut")):
        return {}
    out = {}
    method = b.get("BrushInOutType", 0)
    if b.get("BrushUseIn"):
        if method == 0:
            length = max(1, round(b.px("BrushInLength")))
            for pid, minimum in targets.items():
                if pid in (1001, 1021):
                    out[pid] = Sensor("distance", [(0, minimum), (1, 1)], {"periodic": "0", "length": str(length)})
            rep.add(f"Start {length} px on {sorted(targets)}", "Distance sensor", "exact")
        else:
            rep.add(f"Start by {'percentage' if method == 1 else 'fade'}", "—", "dropped",
                    "Krita doesn't know the stroke length in advance")
    if b.get("BrushUseOut"):
        rep.add("End of stroke taper", "—", "dropped", "Krita can't taper the end of a stroke")
    if b.get("BrushInOutBySpeed"):
        rep.add("Start/end by speed", "—", "dropped")
    return out


def _rotation(b, p, rep, spray):
    """Returns (rotation option on, tip angle in radians). See the plan, section 2b."""
    bits = (b.get("BrushRotationEffector") or 0) & 0xF0
    amount = b.get("BrushRotationRandomScale", 0) / 100
    if spray:
        sp = b.get("BrushRotationEffectorInSpray") or 0
        for sbit, rbit in SPRAY_IN_ROTATION.items():
            if sp & sbit:
                bits |= rbit
        if sp & 0x80:
            amount = b.get("BrushRotationRandomInSpray", 0) / 100
        if sp & 0x100:
            rep.add("Spray particle direction: whole spray", "tip angle", "approx")
        if sp & 0x200:
            rep.add("Spray particle direction: toward centre", "—", "dropped")
    tip_angle = math.radians(b.get("BrushRotation", 0.0))
    if not bits:
        p.update(dyn.option_params("Rotation", [], enabled=False))
        return False, tip_angle

    tracking = []
    if bits & 0x10:
        tracking.append(Sensor("ascension", dyn.LINEAR))
    if bits & 0x20:
        tracking.append(Sensor("rotation", dyn.LINEAR))
    sensors = list(tracking)
    if bits & 0x40:
        sensors.append(Sensor("drawingangle", dyn.LINEAR, {"fanCornersEnabled": "0", "fanCornersStep": "30",
                                                          "angleOffset": "0", "lockedAngleMode": "0"}))
    strength = 1.0
    desc = [f"tip {math.degrees(tip_angle):g}°"]
    if bits & 0x10:
        desc.append("pen tilt direction")
    if bits & 0x20:
        desc.append("barrel rotation")
    if bits & 0x40:
        desc.append("line direction")
    if bits & 0x80:
        if tracking:
            # one strength scales every additive sensor, so the jitter width goes in the fuzzy curve instead
            sensors.append(Sensor("fuzzy", [(0, 0.5 - amount / 2), (1, 0.5 + amount / 2)]))
        else:
            sensors.append(Sensor("fuzzy", dyn.LINEAR))
            strength = amount
        desc.append(f"uniform ±{amount * 180:g}°")
    p.update(dyn.option_params("Rotation", sensors, value=strength, enabled=True))
    if not bits & 0x40:
        tip_angle += ROTATION_OPTION_OFFSET
    else:
        tip_angle += LINE_DIRECTION_OFFSET
    if bits & 0x10:
        tip_angle += TILT_DIRECTION_OFFSET
    rep.add("Angle inputs: " + ", ".join(desc[1:]), "Rotation: " + " + ".join(desc), "approx" if bits & 0x80 else "exact",
            "random amount assumed to be ±RandomScale% of 180°" if bits & 0x80 else "")
    return True, tip_angle


def _brush_definition(b, preset, rep, diameter, spacing, angle, tip_max):
    common = (f'spacing="{dyn._num(spacing)}" useAutoSpacing="0" autoSpacingCoeff="1" '
              f'angle="{dyn._num(angle % (2 * math.pi))}" BrushVersion="2"')
    if not b.get("BrushUsePatternImage") or not b.tips:
        hardness = max(b.get("BrushHardness", 100), 1) / 100
        ratio = 1.0
        aa = "1" if b.get("AntiAlias", 1) else "0"
        rep.add(f"Circle tip, hardness {hardness:.0%}", "auto brush circle", "approx", "hardness → fade")
        return (f'<Brush type="auto_brush" density="1" randomness="0" {common}>'
                f'<MaskGenerator id="default" type="circle" diameter="{dyn._num(diameter)}" ratio="{ratio}" '
                f'hfade="{dyn._num(hardness)}" vfade="{dyn._num(hardness)}" spikes="2" antialiasEdges="{aa}"/>'
                f'</Brush>'), {}

    tips = b.tips
    color = any(tipmod.is_color(t) for t in tips)
    longest = max(32, math.ceil(diameter * 1.25))
    if tip_max:
        longest = min(longest, tip_max)
    longest = min(longest, max(max(t.width, t.height) for t in tips))
    base = resource_name(b.name)
    application = 1 if color else 0
    if len(tips) == 1:
        t = tipmod.resize(tips[0], longest)
        data = tipmod.png(t.width, t.height, t.rgba if color else tipmod.mask_rgba(t))
        fname, btype, cell = f"{base}.png", "png_brush", max(t.width, t.height)
        rep.add("1 material tip", f"{fname} ({t.width}×{t.height})", "exact")
    else:
        order = b.get("BrushPatternOrderType2", b.get("BrushPatternOrderType", 0))
        selection, pingpong, exact = tipmod.REPEAT_MODES.get(order, ("incremental", False, False))
        cells, w, h = tipmod.normalise(tips, longest)
        if pingpong:
            cells = tipmod.ping_pong(cells)
        data = tipmod.gih(b.name, cells, selection, color)
        fname, btype, cell = f"{base}.gih", "gbr_brush", max(w, h)
        rep.add(f"{len(tips)} material tips, repeat method {order}",
                f"{fname}: {len(cells)} cells {w}×{h}, sel0:{selection}" + (" (ping-pong)" if pingpong else ""),
                "exact" if exact else "approx",
                "" if exact else "Krita keeps cycling; it can't stop after one pass")
    md5 = preset.embed("brushes", fname, data)
    scale = diameter / cell
    if color:
        rep.add("Colour tip", "image stamp (brushApplication 1)", "exact")
    return (f'<Brush type="{btype}" filename="{fname}" md5sum="{md5}" scale="{dyn._num(scale)}" '
            f'brushApplication="{application}" ColorAsMask="{0 if color else 1}" {common}/>'), {fname: data}


def texture_pattern(b):
    """The CSP texture as a Krita library pattern: (filename, PNG bytes, the Texture/Pattern/ keys that name it and
    embed it inline as a fallback), or None without a texture."""
    if not b.texture:
        return None
    import base64
    import hashlib
    t = b.texture
    # Krita patterns are plain images: flatten onto white
    px = bytearray(t.rgba)
    for i in range(0, len(px), 4):
        a = px[i + 3]
        for c in range(3):
            px[i + c] = (px[i + c] * a + 255 * (255 - a)) // 255
        px[i + 3] = 255
    data = tipmod.png(t.width, t.height, bytes(px))
    fname = f"{resource_name(b.name)}_texture.png"
    # Inline pattern (KisEmbeddedTextureData::tryLoadEmbeddedPattern), confirmed to load in Krita 5.2 (밀리펜).
    # Also embedding it in <resources> doubled the preset size for no gain.
    keys = {
        "Texture/Pattern/Enabled": "true",
        "Texture/Pattern/Pattern": base64.b64encode(data).decode("ascii"),
        "Texture/Pattern/PatternMD5Sum": hashlib.md5(data).hexdigest(),
        "Texture/Pattern/PatternMD5": base64.b64encode(hashlib.md5(data).digest()).decode("ascii"),
        "Texture/Pattern/PatternFileName": fname,
        "Texture/Pattern/Name": fname[:-4],
    }
    return fname, data, keys


def _texture(b, preset, p, rep, sensors, paintop, modes=None):
    """Krita Pattern texture keys. `modes` (CSP mode -> (Krita mode, note)) replaces the Pixel brush's mapping.
    Returns (pattern filename, PNG bytes), or None without a texture."""
    pattern = texture_pattern(b)
    if not pattern:
        p["Texture/Pattern/Enabled"] = "false"
        return None
    fname, data, keys = pattern
    t = b.texture
    mode = b.get("TextureCompositeMode", 0)
    density = b.get("TextureDensity", 100) / 100
    invert = bool(b.get("TextureReverseDensity"))
    if modes is not None:
        kmode, note = modes.get(mode, (0, f"{TEXTURE_MODE_NAMES[mode] if mode < 9 else mode} → Multiply (default)"))
    elif paintop == "paintbrush" and (mode in TEXTURE_LIGHTNESS_MODES or mode not in TEXTURE_MODES):
        kmode, note = LIGHTNESS_MAP, f"{TEXTURE_MODE_NAMES[mode] if mode < 9 else mode} → Lightness Map"
    else:
        kmode, note = TEXTURE_MODES.get(mode, (0, f"unknown mode {mode} → Multiply"))
    p.update(keys)
    p.update({
        "Texture/Pattern/Scale": dyn._num(b.get("TextureScale2", 100) / 100),
        "Texture/Pattern/Brightness": dyn._num(b.get("TextureBrightness", 0) / 100),
        "Texture/Pattern/Contrast": dyn._num(1 + b.get("TextureContrast", 0) / 100),
        "Texture/Pattern/Invert": "true" if invert else "false",
        "Texture/Pattern/TexturingMode": str(kmode),
        "Texture/Pattern/CutoffPolicy": "0", "Texture/Pattern/CutoffLeft": "0", "Texture/Pattern/CutoffRight": "255",
        "Texture/Pattern/OffsetX": "0", "Texture/Pattern/OffsetY": "0",
        "Texture/Pattern/MaximumOffsetX": str(t.width), "Texture/Pattern/MaximumOffsetY": str(t.height),
        "Texture/Pattern/isRandomOffsetX": "false", "Texture/Pattern/isRandomOffsetY": "false",
        "Texture/Pattern/NeutralPoint": "0.5",
    })
    p.update(dyn.option_params("Texture/Strength/", sensors("TextureDensity", "Texture strength"),
                               value=density, enabled=True))
    rep.add(f"Texture {t.width}×{t.height}, mode {TEXTURE_MODE_NAMES[mode] if mode < 9 else mode}, "
            f"density {b.get('TextureDensity')}", f"embedded pattern, TexturingMode {kmode}",
            "approx", note or "Krita textures don't match CSP's density masking; approximate")
    if b.get("TextureStressDensity"):
        rep.add("Texture: emphasize density", "—", "dropped")
    if not b.get("TextureForPlot"):
        rep.add("Texture applied per stroke", "per dab", "approx")
    return fname, data


def _color_smudge(b, p, rep, sensors):
    paint = b.get("BrushMixColor", 100) / 100
    stretch = b.get("BrushMixColorExtension", 50) / 100
    p.update(dyn.option_params("ColorRate", sensors("BrushMixColor", "ColorRate"), value=paint, enabled=True))
    p.update(dyn.option_params("SmudgeRate", [], value=stretch, enabled=True))
    blur = b.get("BrushBlur", 0) or 0
    blur_sensors = sensors("BrushBlur", "SmudgeRadius")
    if blur:
        radius = min(blur / 100, 1.0) if b.get("BrushBlurLinkSize") else min(blur / max(b.px("BrushSize"), 1), 1.0)
        p.update(dyn.option_params("SmudgeRadius", blur_sensors, value=radius, enabled=True))
    else:
        p.update(dyn.option_params("SmudgeRadius", [], enabled=False))
    mode = b.get("BrushWaterColor", 0)
    p["SmudgeRateMode"] = "0" if mode == 2 else "1"           # Krita 0 = smearing, 1 = dulling
    rep.add(f"Colour mixing mode {mode}: paint {paint:.0%}, stretch {stretch:.0%}, blur {blur:g}",
            f"Color Smudge: ColorRate {paint:g}, SmudgeRate {stretch:g}, "
            f"{'smearing' if mode == 2 else 'dulling'}", "approx",
            "the engines blend differently; tune by eye")


def _color_jitter(b, p, rep):
    """CSP colour jitter -> Krita's HSV options and Mix (sub colour), with Fuzzy Dab / Fuzzy Stroke sensors.
    Per-dab and per-stroke jitter of one channel share Krita's option: both sensors, the larger amount."""
    channels = {}
    for flag, sensor_id, keys in (("BrushChangePatternColor", "fuzzy",
                                   (("h", "BrushHueChange", 180), ("s", "BrushSaturationChange", 100),
                                    ("v", "BrushValueChange", 100))),
                                  ("BrushChangeStrokeColor", "fuzzystroke",
                                   (("h", "BrushStrokeHueChange", 180), ("s", "BrushStrokeSaturationChange", 100),
                                    ("v", "BrushStrokeValueChange", 100)))):
        if not b.get(flag):
            continue
        for opt, key, scale in keys:
            amount = b.get(key, 0)
            if amount:
                sensors, value = channels.get(opt, ([], 0))
                channels[opt] = (sensors + [Sensor(sensor_id, dyn.LINEAR)], max(value, amount / scale))
    jitter = []
    for opt, (sensors, value) in channels.items():
        p.update(dyn.option_params(opt, sensors, value=value, enabled=True))
        jitter.append(f"{opt} {value:g}" + (" (per dab and per stroke)" if len(sensors) > 1 else
                                              " per stroke" if sensors[0].id == "fuzzystroke" else ""))
    if jitter:
        rep.add("Colour jitter: " + ", ".join(jitter), "HSV options (fuzzy)", "approx")

    # Sub colour: up to N% of the sub colour mixed in (Krita's Mix: 1 = main colour)
    mix = []
    for flag, sensor_id, key in (("BrushChangePatternColor", "fuzzy", "BrushSubColor"),
                                 ("BrushChangeStrokeColor", "fuzzystroke", "BrushStrokeSubColor")):
        amount = min(max(b.get(key, 0) or 0, 0), 100) / 100
        if b.get(flag) and amount:
            mix.append(Sensor(sensor_id, [(0.0, 1.0), (1.0, 1.0 - amount)]))
    if mix:
        p.update(dyn.option_params("Mix", mix, value=1.0, enabled=True))
        rep.add("Sub colour mix", "Mix option (fuzzy)", "approx")


def _dual_brush(b, p, rep):
    if not b.get("UseDualBrush"):
        p["MaskingBrush/Enabled"] = "false"
        return
    rep.add("Dual brush", "—", "dropped", "Masked Brush conversion not implemented yet")


def _safe(name):
    """File name for the user's own output folder (Unicode is fine there)."""
    import re
    return re.sub(r'[\\/:*?"<>|;]+', "_", name).strip() or "brush"


def resource_name(name):
    """ASCII file name for files Krita stores (embedded tips/patterns, bundle entries): zip readers and Krita's
    resource folder don't reliably keep non-ASCII names. A short hash keeps converted names distinct."""
    import hashlib
    import re
    ascii_part = re.sub(r"[^A-Za-z0-9._()-]+", "_", name).strip("_.")
    if ascii_part == name and ascii_part:
        return ascii_part
    tag = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    return f"{ascii_part}_{tag}" if re.search(r"[A-Za-z0-9]", ascii_part) else f"csp_{tag}"
