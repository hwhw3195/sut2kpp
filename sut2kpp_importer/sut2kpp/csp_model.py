# Read a Clip Studio Paint .sut sub tool into plain Python values the mapping can use.
# Everything binary is decoded with the included sut_inspect.py; this module only gives fields names and units

from dataclasses import dataclass, field
from pathlib import Path

from . import sut_inspect as si

INPUT_BITS = {"pressure": 0x10, "tilt": 0x20, "velocity": 0x40, "random": 0x80}


@dataclass
class Effector:
    """CSP dynamics popup of one setting: which inputs are on, their minimums (0-1) and stored curves."""
    enabled: list  # subset of "pressure", "tilt", "velocity", "random", in that order
    mins: dict  # input -> minimum as a fraction (random may be negative for colour jitter)
    curves: dict  # input -> [(x, y), ...]; missing = linear
    tilt_max: int = 100

    def __bool__(self):
        return bool(self.enabled)


@dataclass
class Tip:
    name: str
    width: int
    height: int
    rgba: bytes


@dataclass
class CspBrush:
    name: str
    version: int
    v: dict   # raw current Variant row, for anything not given a name below
    effectors: dict = field(default_factory=dict)   # column name without "Effector" -> Effector
    tips: list = field(default_factory=list)   # brush tips in BrushPatternImageArray order (duplicates kept)
    texture: Tip = None
    icon_png: bytes = None
    pressure_graph: list = None  # Manager.PressureGraph points, only when PressureGraphInitialized
    unit_notes: list = field(default_factory=list)

    def get(self, key, default=None):
        value = self.v.get(key)
        return default if value is None else value

    def px(self, key):
        """Length setting in pixels. Unit 0 is px; physical units are stored at CSP's fixed 10 px per mm
        (raw x 100), which is what CSP itself uses independent of canvas dpi (inferred)."""
        raw, unit = self.v.get(key), self.v.get(key + "Unit") or 0
        if raw is None:
            return None
        if unit:
            self.unit_notes.append(f"{key}: unit {unit}, converted at 10 px/mm")
            return raw * 100
        return raw

    def effector(self, setting):
        return self.effectors.get(setting) or Effector([], {}, {})


def parse_effector(blob):
    if not isinstance(blob, bytes) or not si.is_effector("Effector", blob):
        return None
    e = si.decode_effector(blob)
    p = e["params"]
    mask = int(e["enabled_mask"], 16)
    curves, it = {}, iter(e["curves"])
    for name, size_key in (("pressure", "pressure_curve_bytes"), ("tilt", "tilt_curve_bytes"),
                           ("velocity", "velocity_curve_bytes")):
        if p.get(size_key):
            pts = next(it, None)
            if pts:
                curves[name] = [tuple(pt) for pt in pts]
    return Effector(enabled=[n for n, bit in INPUT_BITS.items() if mask & bit],
                    mins={"pressure": p["pressure_min"] / 100, "tilt": p["tilt_min"] / 100,
                          "velocity": p["velocity_min"] / 100, "random": p["random_min"] / 100},
                    curves=curves, tilt_max=p.get("tilt_max", 100))


def _materials(db):
    """MaterialFile.OriginalPath -> decoded Tip."""
    out = {}
    if "MaterialFile" not in si.tables(db):
        return out
    for r in si.rows(db, "MaterialFile"):
        for member, data in si.tar_members(r["FileData"]):
            if member.endswith(".layer"):
                w, h, rgba, _ = si.layer_to_rgba(data)
                out[r["OriginalPath"]] = Tip("", w, h, rgba)
    return out


def _tip_list(v, col, materials):
    blob = v.get(col)
    if not isinstance(blob, bytes) or len(blob) < 8 or si.i32(blob, 0) != 8:
        return []
    tips = []
    for entry in si.decode_pattern_array(blob):
        m = materials.get(entry["path"])
        if m is not None:
            tips.append(Tip(entry.get("name", ""), m.width, m.height, m.rgba))
    return tips


def load(path):
    db = si.connect(path)
    name, v = si.current_variant(db)
    manager = si.rows(db, "Manager")[0]
    b = CspBrush(name=name or Path(path).stem, version=manager.get("Version"), v=v)
    for col, value in v.items():
        if col.endswith("Effector"):
            e = parse_effector(value)
            if e is not None:
                b.effectors[col[:-len("Effector")]] = e
    materials = _materials(db)
    b.tips = _tip_list(v, "BrushPatternImageArray", materials)
    textures = _tip_list(v, "TextureImage", materials)
    b.texture = textures[0] if textures else None
    b.dual_tips = _tip_list(v, "DualPatternImageArray", materials)
    icon = db.execute("select NodeCustomIcon from Node").fetchone()[0]
    if isinstance(icon, bytes) and icon[:8] == b"\x89PNG\r\n\x1a\n":
        b.icon_png = icon
    pg = manager.get("PressureGraph")
    if isinstance(pg, bytes) and manager.get("PressureGraphInitialized"):
        lst, _ = si.read_list(pg, 0)
        if lst:
            b.pressure_graph = [tuple(p) for p in lst["items"]]
    return b
