# CSP effector (dynamics popup) -> Krita curve-option sensors.
#
# Krita (KisCurveOption) multiplies the outputs of "scaling" sensors (pressure, speed, tilt, distance) and
# applies "additive" ones (fuzzy) as uniform random values pushed through their curve, so for size-like options
# every sensor's curve is simply its output multiplier in 0-1. CSP's per-input minimum has no Krita
# counterpart, so it is baked into the curve: y' = min + (1 - min) * y.

import math
from dataclasses import dataclass

LINEAR = [(0.0, 0.0), (1.0, 1.0)]
DEFAULT_CURVE = "0,0;1,1;"

# Tilt. CSP's curve x: 0 = pen lying flat (the popup's left end), 1 = upright, over 90 degrees of lean; its default
# curve falls, so the setting is at its minimum upright and at the effector's tilt maximum (100-1000%) lying flat.
# A pen leans at most about 60 degrees, so CSP never gets all the way to the maximum (CSP strokes, 2026-11-20: about
# 42% of the way at the furthest lean). Krita's "declination" sensor runs the same way but over 60 degrees
# (KisPaintInformation::tiltElevation(info, 60, 60)), so tilt curves are resampled onto its axis.
CSP_TILT_RANGE = 90.0
KRITA_TILT_RANGE = 60.0
TILT_DEFAULT = [(0.0, 1.0), (1.0, 0.0)]
TILT_SAMPLES = 17

# Velocity. CSP: the setting is at 100% when drawing slowly and at its minimum when fast (confirmed on a tablet),
# i.e. CSP's curve x runs fast (0) -> slow (1). Krita's speed sensor is min(1, screen px per ms / 30)
# (KisPaintingInformationBuilder, kritarc maxAllowedSpeedValue = 30), so everyday strokes (0.5-5 px/ms) only
# reach 0.02-0.17. Each Krita x is therefore turned back into a speed and mapped onto CSP's axis logarithmically:
#   fastness u = min(1, ln(1 + v / knee) / ln(1 + fast / knee)),  CSP x = 1 - u
KRITA_MAX_SPEED = 30.0          # px/ms at Krita speed sensor = 1
VELOCITY_FAST = 2.0             # px/ms that counts as fully "fast" in CSP (chosen from calibration S presets)
VELOCITY_KNEE = 0.3             # px/ms where the logarithmic response bends
VELOCITY_SAMPLES = [0, 0.002, 0.005, 0.01, 0.02, 0.04, 0.08, 0.15, 0.3, 1]

SENSOR_IDS = {"pressure": "pressure", "tilt": "declination", "velocity": "speed", "random": "fuzzy"}


@dataclass
class Sensor:
    id: str
    points: list  # [(x, y), ...] in 0-1
    attrs: dict = None   # extra XML attributes (length/periodic for distance, drawing angle options)
    corners: tuple = ()   # x of points written as corners ("x,y,is_corner;", Krita 5.3+)

    @property
    def curve(self):
        return curve_str(self.points, self.corners)


def clean(points):
    """Clamp to 0-1, sort by x, merge points that share an x (CSP repeats corner points; a Krita cubic spline
    needs strictly increasing x), and make sure the curve spans x = 0..1."""
    pts = sorted((min(max(float(x), 0.0), 1.0), min(max(float(y), 0.0), 1.0)) for x, y in points or LINEAR)
    out = []
    for x, y in pts:
        if out and abs(x - out[-1][0]) < 1e-4:
            out[-1] = (out[-1][0], max(out[-1][1], y))
        else:
            out.append((x, y))
    if out[0][0] > 0:
        out.insert(0, (0.0, out[0][1]))
    if out[-1][0] < 1:
        out.append((1.0, out[-1][1]))
    if len(out) == 1:
        out.append((1.0, out[0][1]))
    return out


def with_min(points, minimum):
    m = min(max(minimum, 0.0), 1.0)
    return [(x, m + (1 - m) * y) for x, y in points]


def with_range(points, minimum, maximum):
    """Curve output 0-1 mapped onto minimum..maximum (tilt: the maximum may be above 1)."""
    m = min(max(minimum, 0.0), 1.0)
    return [(x, m + (max(maximum, m) - m) * y) for x, y in points]


def csp_tilt_x(krita_x):
    """CSP tilt-curve x (90 degree range) for a Krita declination value (60 degree range), for a lean along one
    axis: Krita's declination is acos(lean / 60) / (pi / 2)."""
    lean = KRITA_TILT_RANGE * math.cos(min(max(krita_x, 0.0), 1.0) * math.pi / 2)
    return math.acos(min(1.0, lean / CSP_TILT_RANGE)) / (math.pi / 2)


def tilt_curve(points):
    """CSP tilt curve (output 0-1) resampled onto Krita's declination axis."""
    return [(i / (TILT_SAMPLES - 1), evaluate(points, csp_tilt_x(i / (TILT_SAMPLES - 1))))
            for i in range(TILT_SAMPLES)]


def headroom(sensors):
    """(sensors, gain): a sensor whose curve rises above 1 (a tilt maximum over 100%) scaled back into 0-1, and the
    factor the option's base value (the diameter, for size) has to grow by to keep the same result."""
    gain = max([1.0] + [y for s in sensors for _, y in s.points])
    if gain <= 1.0:
        return sensors, 1.0
    return [Sensor(s.id, [(x, y / gain) for x, y in s.points], s.attrs, s.corners)
            if max(y for _, y in s.points) > 1.0 else s for s in sensors], gain


def evaluate(points, x):
    """Piecewise-linear value of a curve (used for composing curves and for the report; Krita itself
    interpolates with a cubic spline through the same points)."""
    pts = clean(points)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def compose(outer, inner, samples=17):
    """outer(inner(x)) resampled; used for --apply-tool-pressure."""
    return [(i / (samples - 1), evaluate(outer, evaluate(inner, i / (samples - 1)))) for i in range(samples)]


def curve_str(points, corners=()):
    def corner(x):
        return any(abs(x - c) < 1e-4 for c in corners)
    return "".join(f"{_num(x)},{_num(y)}{',is_corner' if corner(x) else ''};" for x, y in clean(points))


def repeated_x(points):
    """x of the points CSP stores twice: CSP's way of making a sharp corner in a smooth curve."""
    xs = sorted(round(float(x), 4) for x, _ in points or [])
    return tuple(sorted({a for a, b in zip(xs, xs[1:]) if a == b}))


def _num(v):
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def csp_velocity_x(krita_x, fast=None, knee=None):
    """CSP velocity-curve x (1 = slow) for a Krita speed sensor value."""
    fast, knee = fast or VELOCITY_FAST, knee or VELOCITY_KNEE
    v = krita_x * KRITA_MAX_SPEED
    u = min(1.0, math.log1p(v / knee) / math.log1p(fast / knee))
    return 1.0 - u


def velocity_curve(points, fast=None, knee=None):
    """CSP velocity curve (minimum already baked in) resampled onto Krita's speed axis."""
    return [(x, evaluate(points, csp_velocity_x(x, fast, knee))) for x in VELOCITY_SAMPLES]


def effector_sensors(eff, tool_pressure=None, speed=(None, None)):
    """Krita sensors for the inputs enabled in a CSP Effector (size-like settings).
    `speed` = (fast px/ms, knee px/ms) overrides for the velocity mapping. A tilt maximum over 100% leaves the tilt
    curve above 1: Krita clamps it when written, unless headroom() moves the excess into the option's base value."""
    sensors = []
    for name in eff.enabled:
        if name == "random":
            pts = with_min(LINEAR, eff.mins["random"])
        elif name == "tilt":
            # resampled before the range is applied: evaluate() clamps to 0-1
            pts = tilt_curve(clean(eff.curves.get("tilt", TILT_DEFAULT)))
            pts = with_range(pts, eff.mins["tilt"], max(1.0, eff.tilt_max / 100))
        else:
            pts = clean(eff.curves.get(name, LINEAR))
            if name == "pressure" and tool_pressure:
                pts = compose(pts, tool_pressure)
            pts = with_min(pts, eff.mins[name])
            if name == "velocity":
                pts = velocity_curve(pts, *speed)
        sensors.append(Sensor(SENSOR_IDS[name], pts))
    return sensors


def sensor_xml(sensors):
    """The <XSensor> value, in the shape KisKritaSensorPack::write produces."""
    def body(s):
        attrs = "".join(f' {k}="{v}"' for k, v in (s.attrs or {}).items())
        curve = "" if s.curve == DEFAULT_CURVE else f"\n  <curve>{s.curve}</curve>\n "
        return attrs, curve
    if len(sensors) == 1:
        attrs, curve = body(sensors[0])
        inner = f'<params id="{sensors[0].id}"{attrs}>{curve}</params>' if curve else f'<params id="{sensors[0].id}"{attrs}/>'
        return f"<!DOCTYPE params>\n{inner}\n"
    children = []
    for s in sensors:
        attrs, curve = body(s)
        children.append(f' <ChildSensor id="{s.id}"{attrs}>{curve}</ChildSensor>' if curve
                        else f' <ChildSensor id="{s.id}"{attrs}/>')
    return '<!DOCTYPE params>\n<params id="sensorslist">\n' + "\n".join(children) + "\n</params>\n"


def option_params(option, sensors, value=1.0, enabled=None, curve_mode=0):
    """All keys of one Krita curve option. With several sensors each keeps its own curve (UseSameCurve off).
    No sensors = constant `value`: the option stays checked when `enabled` but UseCurve is off, which makes
    KisCurveOption ignore its sensors (Size/Opacity/Flow are always checked in Krita 5)."""
    use_curve = bool(sensors)
    if enabled is None:
        enabled = use_curve
    if not sensors:
        sensors = [Sensor("pressure", LINEAR)]
    same = len(sensors) == 1
    return {
        f"Pressure{option}": _bool(enabled),
        f"{option}Sensor": sensor_xml(sensors),
        f"{option}UseCurve": _bool(use_curve),
        f"{option}UseSameCurve": _bool(same),
        f"{option}commonCurve": sensors[0].curve if same else DEFAULT_CURVE,
        f"{option}curveMode": str(curve_mode),
        f"{option}Value": _num(value),
    }


def merge(a, b, samples=17):
    """Combine two sensor lists that drive one Krita option (e.g. density folded into opacity). Sensors with
    the same id can't appear twice, so their curves are multiplied pointwise."""
    out = {s.id: s for s in a}
    for s in b:
        if s.id in out:
            o = out[s.id]
            if s.curve == "1,1;" or s.curve == "0,1;1,1;":
                continue
            if o.curve == "0,1;1,1;":
                out[s.id] = s
                continue
            pts =[(i / (samples - 1), evaluate(o.points, i / (samples - 1)) * evaluate(s.points, i / (samples - 1)))
                   for i in range(samples)]
            out[s.id] = Sensor(s.id, pts, o.attrs or s.attrs)
        else:
            out[s.id] = s
    return list(out.values())


def _bool(b):
    return "true" if b else "false"
