#!/usr/bin/env python3
# Inspect, describe, diff and extract Clip Studio Paint .sut sub tool files. Opens files read-only.
# Python 3.8+, standard library only. Where Python has no sqlite3 module (some Linux builds of Krita),
# files are read through Qt's SQLite driver (QtSql of PyQt6 or PyQt5) instead.
#
#   python3 sut_inspect.py describe brush.sut         # settings with CSP names and decoded values
#   python3 sut_inspect.py decode   brush.sut         # every field as JSON, binary fields decoded
#   python3 sut_inspect.py dump     brush.sut         # raw non-NULL fields
#   python3 sut_inspect.py diff     a.sut b.sut       # fields that differ between two exports
#   python3 sut_inspect.py extract  brush.sut outdir  # brush tips at full size (+ thumbnails, xml)

import importlib
import io
import json
import re
import struct
import sys
import tarfile
import zlib
from pathlib import Path

try:
    import sqlite3
except ImportError:         # Python built without the _sqlite3 extension
    sqlite3 = None


# ---------------------------------------------------------------- basics

def _qt():
    # (QtCore, QtSql) of the PyQt already loaded in this process, else PyQt6, else PyQt5.
    names = ("PyQt6", "PyQt5")
    for name in [n for n in names if n + ".QtCore" in sys.modules] or names:
        try:
            return importlib.import_module(name + ".QtCore"), importlib.import_module(name + ".QtSql")
        except ImportError:
            continue
    raise ImportError("cannot read .sut files: this Python has neither sqlite3 nor PyQt's QtSql")


class _QtCursor:
    # The part of sqlite3.Cursor this package uses: description, fetchone, fetchall, iteration.
    def __init__(self, columns, rows):
        self.description = [(c,) for c in columns]
        self._rows = iter(rows)

    def fetchone(self):
        return next(self._rows, None)

    def fetchall(self):
        return list(self._rows)

    def __iter__(self):
        return self._rows


class _QtConnection:
    # Read-only stand-in for sqlite3.Connection on Qt's SQLite driver.
    _serial = 0

    def __init__(self, path):
        self._name = None
        self._core, self._sql = _qt()
        database = self._sql.QSqlDatabase
        _QtConnection._serial += 1
        # a named connection: the unnamed default one is Krita's own resource database
        name = f"sut2kpp-{_QtConnection._serial}"
        db = database.addDatabase("QSQLITE", name)
        db.setDatabaseName(str(Path(path).resolve()))
        db.setConnectOptions("QSQLITE_OPEN_READONLY")
        if not db.open():
            message = db.lastError().text()
            del db
            database.removeDatabase(name)
            raise OSError(f"cannot open {path}: {message}")
        self._db, self._name = db, name

    def execute(self, sql, params=()):
        byte_array = self._core.QByteArray
        query = self._sql.QSqlQuery(self._db)
        query.setForwardOnly(True)
        if not query.prepare(sql):
            raise RuntimeError(f"{query.lastError().text()}: {sql}")
        for p in params:
            query.addBindValue(byte_array(p) if isinstance(p, bytes) else p)
        if not query.exec():
            raise RuntimeError(f"{query.lastError().text()}: {sql}")
        record = query.record()
        count = record.count()
        columns = [record.fieldName(i) for i in range(count)]
        rows = []
        while query.next():
            row = []
            for i in range(count):
                value = query.value(i)
                if isinstance(value, byte_array):
                    value = value.data()        # BLOB -> bytes, as sqlite3 gives
                elif query.isNull(i):
                    value = None
                row.append(value)
            rows.append(tuple(row))
        return _QtCursor(columns, rows)

    def close(self):
        if self._name:
            self._db.close()
            self._db = None
            self._sql.QSqlDatabase.removeDatabase(self._name)
            self._name = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def connect(path):
    if sqlite3 is not None:
        return sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    return _QtConnection(path)


def tables(db):
    return [r[0] for r in db.execute("select name from sqlite_master where type='table' and name!='sqlite_sequence'")]


def rows(db, table):
    cur = db.execute(f'select * from "{table}"')
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def i32(b, o):
    return struct.unpack_from(">i", b, o)[0]


def utf16le(b):
    return b.decode("utf-16-le", "replace").rstrip("\x00")


def current_variant(db):
    name, vid = db.execute("select NodeName, NodeVariantID from Node").fetchone()
    cur = db.execute("select * from Variant where VariantID=?", (vid,))
    return name, dict(zip([d[0] for d in cur.description], cur.fetchone()))


# ---------------------------------------------------------------- binary column formats

def read_list(b, o):
    # [12][count][item_size] + items; points are (x, y) float64 pairs
    if o + 12 > len(b):
        return None, o
    hdr, n, size = i32(b, o), i32(b, o + 4), i32(b, o + 8)
    if hdr != 12 or not (0 <= n <= 10000) or size <= 0 or o + hdr + n * size > len(b):
        return None, o
    items, p = [], o + hdr
    for _ in range(n):
        chunk = b[p:p + size]
        if size == 16:
            items.append([round(x, 4) for x in struct.unpack(">dd", chunk)])
        elif size % 4 == 0:
            items.append([i32(chunk, j) for j in range(0, size, 4)] if size > 4 else i32(chunk, 0))
        else:
            items.append(chunk.hex())
        p += size
    return {"item_size": size, "items": items}, p


INPUTS = {0x10: "pen pressure (필압)", 0x20: "tilt (기울기)", 0x40: "velocity (속도)", 0x80: "random (랜덤)",
          0x100: "0x100 (unknown)"}
# [5] [6] [8] are byte sizes of the stored curve lists (pressure, tilt, velocity); [8] exists only in v145
EFFECTOR_PARAMS = ["pressure_min", "tilt_min", "velocity_min", "random_min", "p4", "pressure_curve_bytes",
                   "tilt_curve_bytes", "tilt_max", "velocity_curve_bytes"]


def bits(mask, names):
    return [name for bit, name in names.items() if mask & bit]


def decode_effector(b):
    # header: [header_len 44 (v<=142) or 48 (v145)][available mask][enabled mask][params...], then curve lists
    h = i32(b, 0)
    head = [i32(b, o) for o in range(0, h, 4)]
    out = {"available": hex(head[1]), "enabled_mask": hex(head[2]), "enabled": bits(head[2], INPUTS),
           "params": dict(zip(EFFECTOR_PARAMS, head[3:])), "curves": []}
    p = h
    while p < len(b):
        lst, p2 = read_list(b, p)
        if lst is None:
            out["tail_hex"] = b[p:].hex()
            break
        out["curves"].append(lst["items"])
        p = p2
    return out


def is_effector(col, b):
    return col.endswith("Effector") and len(b) >= 44 and i32(b, 0) in (44, 48)


def decode_pattern_array(b):
    # [8][count] per tip: [record_len][n][UTF-16LE path] then (tag, n, UTF-16LE) pairs until tag 0
    # tag 1 = material catalog path, tag 2/3 = tip name. path == MaterialFile.OriginalPath
    tips, p = [], 8
    for _ in range(i32(b, 4)):
        rec_len = i32(b, p)
        rec, q = b[p + 4:p + rec_len], 0
        n = i32(rec, q)
        tip = {"path": utf16le(rec[q + 4:q + 4 + n])}
        q += 4 + n
        while q + 8 <= len(rec):
            tag, n = i32(rec, q), i32(rec, q + 4)
            if tag == 0 or n < 0 or q + 8 + n > len(rec):
                break
            tip["name" if tag in (2, 3) else "catalog" if tag == 1 else f"tag{tag}"] = utf16le(rec[q + 8:q + 8 + n])
            q += 8 + n
        tips.append(tip)
        p += rec_len
    return tips


def is_tar(b):
    return len(b) > 262 and b[257:262] == b"ustar"


def tar_members(b):
    with tarfile.open(fileobj=io.BytesIO(b)) as t:
        return [(m.name, t.extractfile(m).read()) for m in t.getmembers() if m.isfile()]


def decode_blob(col, b):
    if col.endswith("Uuid"):
        return b.hex() if len(b) == 16 else None
    if is_effector(col, b):
        return decode_effector(b)
    if (col.endswith("PatternImageArray") or col.endswith("TextureImage")) and len(b) >= 8 and i32(b, 0) == 8:
        return decode_pattern_array(b)
    if is_tar(b):
        return {"tar": [{"name": n, "bytes": len(d), "magic": d[:8].hex()} for n, d in tar_members(b)]}
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return {"png_bytes": len(b)}
    lst, end = read_list(b, 0)
    if lst is not None and end == len(b):
        return lst
    return {"bytes": len(b), "head_hex": b[:32].hex()}


def decoded(path):
    db = connect(path)
    return {t: [{c: (decode_blob(c, v) if isinstance(v, bytes) else v) for c, v in r.items() if v is not None}
                for r in rows(db, t)] for t in tables(db)}


# ---------------------------------------------------------------- settings with names

UNIT_FACTOR = {0: 1, 1: 100, 2: 10, 3: 254}     # value CSP shows = raw * factor (cm / mm / inch at 10 per mm)
ANTIALIAS = {0: "없음 none", 1: "약 weak", 2: "중 medium", 3: "강 strong"}
SPACING = {0: "고정 fixed", 1: "넓음 wide", 2: "보통 normal", 3: "좁음 narrow"}
REPEAT = {0: "반복 repeat", 1: "반환 reverse", 2: "반복 없음 no repeat", 3: "랜덤 random",
          4: "한 바퀴만 돌기 one time only", 5: "랜덤으로 한 바퀴 돌기 one random cycle (inferred)"}
INOUT_TYPE = {0: "길이 지정 by length", 1: "백분율 지정 by percentage", 2: "페이드 fade"}
SPEED_MODE = {0: "재빨리 그렸을때 약한 보정 (reduce when fast)", 1: "천천히 그렸을때 보정 적용 (increase when slow)"}
TIP_DIRECTION = {0x10: "펜 방향 direction of pen", 0x20: "펜축 회전 pen tilt/barrel", 0x40: "선 방향 direction of line",
                 0x80: "랜덤 random"}
SPRAY_DIRECTION = {0x40: "선 방향 direction of line", 0x80: "랜덤 random", 0x100: "살포 전체 방향 whole spray",
                   0x200: "살포 중심에 집중 toward center"}
INOUT_TARGETS = {1001: "브러쉬 크기 brush size", 1021: "브러쉬 농도 brush density"}


def shown(raw, unit):
    return round(raw * UNIT_FACTOR.get(unit, 1), 3) if raw is not None else None


def dynamics(b):
    if not isinstance(b, bytes) or not is_effector("Effector", b):
        return None
    e = decode_effector(b)
    if not e["enabled"]:
        return "none"
    p = e["params"]
    parts = []
    for name in e["enabled"]:
        if name.startswith("pen"):
            parts.append(f"필압 min {p['pressure_min']}")
        elif name.startswith("tilt"):
            parts.append(f"기울기 min {p['tilt_min']} max {p.get('tilt_max')}")
        elif name.startswith("velocity"):
            parts.append(f"속도 min {p['velocity_min']}")
        elif name.startswith("random"):
            parts.append(f"랜덤 min {p['random_min']}")
        else:
            parts.append(name)
    return "; ".join(parts) + f"  ({len(e['curves'])} curve(s))"


def describe(path):
    db = connect(path)
    name, v = current_variant(db)
    m = rows(db, "Manager")[0]
    version = m["Version"]
    g = v.get
    out = {"name": name, "format_version": version}
    pg = m.get("PressureGraph")
    if isinstance(pg, bytes):
        out["tool pressure graph 필압 그래프"] = {"points": read_list(pg, 0)[0]["items"],
                                              "initialized": m.get("PressureGraphInitialized"),
                                              "use_pressure_file": m.get("UsePressureFile")}
    out["브러쉬 크기 brush size"] = {"value_shown": shown(g("BrushSize"), g("BrushSizeUnit")), "raw": g("BrushSize"),
                                  "unit_code": g("BrushSizeUnit"), "dynamics": dynamics(g("BrushSizeEffector")),
                                  "화면상의 크기로 지정 size on screen": bool(g("BrushSizeSyncViewScale")),
                                  "최저 1픽셀 at least 1 pixel": bool(g("BrushAtLeast1Pixel"))}
    out["불투명도 opacity"] = {"value": g("Opacity"), "dynamics": dynamics(g("BrushOpacityEffector"))}
    out["브러쉬 농도 brush density"] = {"value": g("BrushFlow"), "dynamics": dynamics(g("BrushFlowEffector"))}
    out["안티에일리어싱 anti-aliasing"] = ANTIALIAS.get(g("AntiAlias"), g("AntiAlias"))
    out["경도 hardness"] = g("BrushHardness")
    out["두께 thickness"] = {"value": g("BrushThickness"), "dynamics": dynamics(g("BrushThicknessEffector"))}
    rot = g("BrushRotationEffector") or 0
    out["브러쉬 끝 brush tip"] = {
        "shape": "material" if g("BrushUsePatternImage") else "circle",
        "tips": decode_pattern_array(g("BrushPatternImageArray")) if g("BrushPatternImageArray") else [],
        "angle": g("BrushRotation"), "angle_inputs": bits(rot, TIP_DIRECTION),
        "angle_random": g("BrushRotationRandomScale") if rot & 0x80 else None}
    out["획 stroke"] = {"간격 spacing": SPACING.get(g("BrushAutoIntervalType"), g("BrushAutoIntervalType")),
                       "fixed_value": g("BrushInterval") if g("BrushAutoIntervalType") == 0 else None,
                       "spacing_dynamics": dynamics(g("BrushIntervalEffector")),
                       "반복 방법 repeat": REPEAT.get(g("BrushPatternOrderType2"), g("BrushPatternOrderType2")),
                       "ribbon": bool(g("BrushRibbon")), "continuous": bool(g("BrushContinuousPlot"))}
    if g("BrushUseSpray"):
        sp = g("BrushRotationEffectorInSpray") or 0
        out["살포 효과 spray"] = {"particle_size_shown": shown(g("BrushSpraySize"), g("BrushSpraySizeUnit")),
                              "link_to_brush_size": bool(g("BrushSpraySizeSyncBrushSize")),
                              "density": g("BrushSprayDensity"), "bias": g("BrushSprayBias"),
                              "particle_direction": bits(sp, SPRAY_DIRECTION) or ["없음 none"],
                              "direction_random": g("BrushRotationRandomInSpray") if sp & 0x80 else None}
    out["보정 correction"] = {"손떨림 보정 stabilization": g("FlickerReduction"),
                            "속도에 의한 보정 adjust by speed": bool(g("FlickerReductionBySpeed")),
                            "speed_mode": SPEED_MODE.get(g("FlickerReductionBySpeedType")),
                            "선 꼬리 효과 taper": g("Stickness"),
                            "후보정 post correction": g("BrushRevision") if g("BrushUseRevision") else None,
                            "스냅 가능 snapping": bool(g("BrushEnableSnap"))}
    targets = []
    t = g("BrushInOutTarget")
    if isinstance(t, bytes):
        for k in range(i32(t, 4)):
            pid, on, minimum = struct.unpack_from(">3i", t, 12 + 12 * k)
            if on:
                targets.append({"target": INOUT_TARGETS.get(pid, pid), "minimum": minimum})
    out["시작점과 끝점 starting and ending"] = {
        "targets": targets, "method": INOUT_TYPE.get(g("BrushInOutType"), g("BrushInOutType")),
        "start": (shown(g("BrushInLength"), g("BrushInLengthUnit")) if g("BrushInOutType") == 0 else g("BrushInRatio"))
        if g("BrushUseIn") else None,
        "end": (shown(g("BrushOutLength"), g("BrushOutLengthUnit")) if g("BrushInOutType") == 0 else g("BrushOutRatio"))
        if g("BrushUseOut") else None,
        "by_speed": bool(g("BrushInOutBySpeed"))}
    jitter = {}
    if g("BrushChangePatternColor"):
        jitter["per tip 브러쉬 끝 색 변화"] = {
            "hue 색상": g("BrushHueChange"), "hue_dynamics": dynamics(g("BrushHueChangeEffector")),
            "saturation 채도": g("BrushSaturationChange"), "saturation_dynamics": dynamics(g("BrushSaturationChangeEffector")),
            "value 명도": g("BrushValueChange"), "value_dynamics": dynamics(g("BrushValueChangeEffector")),
            "sub color mix 보조 그리기색 혼합율": g("BrushSubColor"), "sub_color_dynamics": dynamics(g("BrushSubColorEffector"))}
    if g("BrushChangeStrokeColor"):
        jitter["per stroke 획마다 변화"] = {"hue": g("BrushStrokeHueChange"), "saturation": g("BrushStrokeSaturationChange"),
                                       "value": g("BrushStrokeValueChange"), "sub color mix": g("BrushStrokeSubColor")}
    if jitter:
        out["색 변화 color jitter"] = jitter
    if g("TextureImage"):
        out["텍스처 texture"] = {"image": decode_pattern_array(g("TextureImage")), "density": g("TextureDensity"),
                               "scale": g("TextureScale2"), "rotate": g("TextureRotate"),
                               "texture_mode": ["Normal", "Multiply", "Subtract", "Compare", "Outline", "Overlay",
                                                "Color dodge", "Hard mix", "Height"][g("TextureCompositeMode") or 0],
                               "invert": bool(g("TextureReverseDensity")), "emphasize": bool(g("TextureStressDensity")),
                               "per_tip": bool(g("TextureForPlot")),
                               "brightness": g("TextureBrightness"), "contrast": g("TextureContrast")}
    if g("BrushUseWaterEdge"):
        after = bool(g("BrushWaterEdgeAfterDrag"))
        out["수채 경계 watercolor edge"] = {
            "range 범위": shown(g("BrushWaterEdgeRadius"), g("BrushWaterEdgeRadiusUnit")),
            "opacity_effect 투명도 영향": g("BrushWaterEdgeAlphaPower"),
            "value_effect 명도 영향": g("BrushWaterEdgeValuePower"),
            "process after drag 드래그 후 처리": after,
            "blur 흐림 폭": shown(g("BrushWaterEdgeBlur"), g("BrushWaterEdgeBlurUnit")) if after else None}
    if g("UseDualBrush"):
        out["2-브러쉬 모양 dual brush"] = {"size": g("DualSize"), "composite_mode": g("DualBrushCompositeMode"),
                                       "sync_size": bool(g("SyncDualBrushSize")), "shape": "material" if g("DualUsePatternImage") else "circle",
                                       "tips": decode_pattern_array(g("DualPatternImageArray")) if g("DualPatternImageArray") else [],
                                       "dynamics": dynamics(g("DualSizeEffector"))}
    if g("BrushUseWaterColor"):
        mode = g("BrushWaterColor")
        out["밑바탕 혼색 color mixing"] = {
            "mode": {0: "색 혼합 blend", 1: "번짐 running color", 2: "색 중첩 smear"}.get(mode, mode),
            "amount of paint 물감량": g("BrushMixColor"), "paint_dynamics": dynamics(g("BrushMixColorEffector")),
            "density of paint 물감 농도": g("BrushMixAlpha"), "density_dynamics": dynamics(g("BrushMixAlphaEffector")),
            "color stretch 색 늘이기": g("BrushMixColorExtension"),
            "blur 흐림 폭": g("BrushBlur"), "blur_link_size": bool(g("BrushBlurLinkSize")),
            "mixing_mode": g("BrushColorMixingMode"), "lms_linearity": g("BrushLMSLinearity")}
    out["other_on"] = [k for k in ("BrushUseVectorEraser",) if g(k)]
    return out


# ---------------------------------------------------------------- dump / diff

def cmd_dump(path):
    db = connect(path)
    for t in tables(db):
        rs = rows(db, t)
        print(f"== {t}: {len(rs)} rows")
        for r in rs:
            for c, v in r.items():
                if v is None:
                    continue
                if isinstance(v, bytes):
                    print(f"  {c}: BLOB[{len(v)}] {v[:48].hex(' ')}{' ...' if len(v) > 48 else ''}")
                else:
                    print(f"  {c}: {v!r}")
            print("  --")


def flatten(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from flatten(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list) and obj and isinstance(obj[0], dict):
        for i, v in enumerate(obj):
            yield from flatten(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def cmd_diff(a, b):
    fa, fb = dict(flatten(decoded(a))), dict(flatten(decoded(b)))
    changed = sorted(k for k in fa.keys() | fb.keys() if fa.get(k) != fb.get(k))
    noise = ("SavedCount", "MaxVariantID", "Uuid", "VariantID")
    for k in changed:
        tag = "  (bookkeeping)" if any(n in k for n in noise) else ""
        print(f"{k}{tag}\n  A: {fa.get(k)}\n  B: {fb.get(k)}")
    if not changed:
        print("No differences.")


# ---------------------------------------------------------------- brush tip images (C2F .layer)
# MaterialFile.FileData is a tar: catalog.zip, info.zip, data/material*.layer (C2F), thumbnail/thumbnail.png,
# icedata/layerData.xml. C2F = signature 89 'C2F' 0D 0A 1A 0A + chunks [u32 LE len][tag][data][u32 LE crc32]:
# HEAD, dATA (scrambled start of an SQLite DB), dATA (the rest of it), TAIL. The DB is CSP's canvas schema;
# pixels are Offscreen.BlockData: 256x256 zlib tiles.

def c2f_chunks(c2f):
    if c2f[:8] != b"\x89C2F\r\n\x1a\n":
        raise ValueError("not a C2F file")
    p, out = 8, []
    while p + 12 <= len(c2f):
        n = struct.unpack_from("<I", c2f, p)[0]
        out.append((c2f[p + 4:p + 8], c2f[p + 8:p + 8 + n]))
        p += 12 + n
    return out


def varint(b, p):
    v = 0
    for i in range(9):
        c = b[p + i]
        if i == 8:
            return (v << 8) | c, p + 9
        v = (v << 7) | (c & 0x7F)
        if c < 0x80:
            return v, p + i + 1


class PageReader:
    # Minimal SQLite table-leaf reader that works without the (scrambled) first page.
    def __init__(self, db, page_size):
        self.db, self.ps = db, page_size

    def payload(self, p, n):
        u = self.ps
        x, m = u - 35, ((u - 12) * 32 // 255) - 23
        if n <= x:
            return self.db[p:p + n]
        local = m + ((n - m) % (u - 4))
        if local > x:
            local = m
        out = bytearray(self.db[p:p + local])
        nxt, rem = struct.unpack(">I", self.db[p + local:p + local + 4])[0], n - local
        while rem > 0 and nxt:
            off = (nxt - 1) * u
            nn = struct.unpack(">I", self.db[off:off + 4])[0]
            take = min(rem, u - 4)
            out += self.db[off + 4:off + 4 + take]
            rem, nxt = rem - take, nn
        return bytes(out)

    @staticmethod
    def record(pl):
        hl, p = varint(pl, 0)
        types = []
        while p < hl:
            t, p = varint(pl, p)
            types.append(t)
        vals, q = [], hl
        for t in types:
            if t == 0:
                vals.append(None)
            elif t in (8, 9):
                vals.append(t - 8)
            elif t in (1, 2, 3, 4, 5, 6):
                n = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8}[t]
                vals.append(int.from_bytes(pl[q:q + n], "big", signed=True))
                q += n
            elif t == 7:
                vals.append(struct.unpack(">d", pl[q:q + 8])[0])
                q += 8
            else:
                n = (t - 12) // 2 if t % 2 == 0 else (t - 13) // 2
                vals.append(pl[q:q + n])
                q += n
        return vals

    def records(self):
        for page in range(2, len(self.db) // self.ps + 1):
            base = (page - 1) * self.ps
            if self.db[base] != 0x0D:
                continue
            n = struct.unpack_from(">H", self.db, base + 3)[0]
            for i in range(n):
                try:
                    cp = base + struct.unpack_from(">H", self.db, base + 8 + 2 * i)[0]
                    size, p = varint(self.db, cp)
                    _, p = varint(self.db, p)
                    yield self.record(self.payload(p, size))
                except Exception:
                    continue


PARAM = "Parameter".encode("utf-16-be")
BLOCK = "BlockDataBeginChunk".encode("utf-16-be")


def tiles(block):
    out = {}
    for m in re.finditer(BLOCK, block):
        e = m.start() + len(BLOCK)
        idx, _, tw, th, present = struct.unpack_from(">5I", block, e)
        if present:
            zl = struct.unpack_from("<I", block, e + 24)[0]
            out[idx] = (tw, th, zlib.decompress(block[e + 28:e + 28 + zl]))
    return out


def offscreens(reader):
    for v in reader.records():
        if len(v) == 6 and isinstance(v[4], bytes) and isinstance(v[5], bytes) and PARAM in v[4]:
            a = v[4]
            i = a.find(PARAM) + len(PARAM)
            w, h, bx, by = struct.unpack_from(">4I", a, i)
            yield w, h, bx, by, v[5]


BITS = [bytes(((b >> (7 - k)) & 1) * 255 for k in range(8)) for b in range(256)]


def layer_to_rgba(c2f):
    data = [d for t, d in c2f_chunks(c2f) if t == b"dATA"]
    db = b"\0" * (len(data[0]) - 12) + data[1]
    for ps in (4096, 1024, 2048):
        try:
            for w, h, bx, by, block in offscreens(PageReader(db, ps)):
                ts = tiles(block)
                if not ts:
                    continue
                W, H = bx * 256, by * 256
                canvas, kinds = bytearray(W * H * 4), set()
                for idx, (tw, th, raw) in ts.items():
                    n = tw * th
                    if len(raw) == n // 4:          # monochrome layer: 1-bit tip plane (+1 unused plane)
                        a, c, kind = b"".join(BITS[x] for x in raw[:n // 8]), None, "1-bit"
                    elif len(raw) == n:             # 8-bit coverage
                        a, c, kind = raw, None, "8-bit"
                    elif len(raw) == 2 * n:         # gray layer: 8-bit alpha plane, then 8-bit gray plane
                        a, c, kind = raw[:n], raw[n:], "8-bit alpha+gray"
                    elif len(raw) == 5 * n:         # color layer: 8-bit alpha plane, then B G R x per pixel
                        a, c, kind = raw[:n], raw[n:], "8-bit alpha+BGRx color"
                    else:
                        kinds.add(f"unknown tile {len(raw)} bytes")
                        continue
                    kinds.add(kind)
                    ty, tx = divmod(idx, bx)
                    for r in range(th):
                        base = ((ty * th + r) * W + tx * tw) * 4
                        end_ = base + 4 * tw
                        canvas[base + 3:end_:4] = a[r * tw:(r + 1) * tw]
                        if c is None:
                            continue
                        if kind.endswith("gray"):
                            row = c[r * tw:(r + 1) * tw]
                            canvas[base:end_:4] = canvas[base + 1:end_:4] = canvas[base + 2:end_:4] = row
                        else:
                            row = c[r * tw * 4:(r + 1) * tw * 4]
                            canvas[base:end_:4], canvas[base + 1:end_:4], canvas[base + 2:end_:4] = row[2::4], row[1::4], row[0::4]
                rgba = b"".join(bytes(canvas[y * W * 4:y * W * 4 + w * 4]) for y in range(h))
                return w, h, rgba, sorted(kinds)
        except Exception:
            continue
    raise ValueError("could not decode tip image")


def write_png(path, w, h, rgba):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def safe(s):
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip() or "tip"


def cmd_extract(path, outdir):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    db = connect(path)
    stem = Path(path).stem
    names = {}
    _, v = current_variant(db)
    for col, role in (("BrushPatternImageArray", "tip"), ("TextureImage", "texture"),
                      ("DualPatternImageArray", "dual-tip"), ("DualTextureImage", "dual-texture")):
        if isinstance(v.get(col), bytes) and len(v[col]) >= 8 and i32(v[col], 0) == 8:
            for t in decode_pattern_array(v[col]):
                names.setdefault(t["path"], f"{role}.{t.get('name', '')}")
    count = 0
    if "MaterialFile" in tables(db):
        for r in rows(db, "MaterialFile"):
            label = safe(f"{stem}.{r['_PW_ID']}.{names.get(r['OriginalPath'], '')}".rstrip("."))
            for member, data in tar_members(r["FileData"]):
                if member.endswith(".layer"):
                    w, h, rgba, kinds = layer_to_rgba(data)
                    write_png(out / f"{label}.png", w, h, rgba)
                    print(f"{label}.png  {w}x{h}  {', '.join(kinds)}")
                    count += 1
                elif member.endswith("thumbnail.png"):
                    (out / f"{label}.thumbnail.png").write_bytes(data)
                elif member.endswith(".xml"):
                    (out / f"{label}.layerData.xml").write_bytes(data)
    icon = db.execute("select NodeCustomIcon from Node").fetchone()[0]
    if isinstance(icon, bytes) and icon[:8] == b"\x89PNG\r\n\x1a\n":
        (out / f"{safe(stem)}.icon.png").write_bytes(icon)
    print(f"{count} image(s) written to {out}")


# ---------------------------------------------------------------- main

def main(argv):
    usage = "usage: sut_inspect.py describe|decode|dump <file> | diff <a> <b> | extract <file> <outdir>"
    if len(argv) < 3:
        sys.exit(usage)
    cmd = argv[1]
    if cmd == "describe":
        print(json.dumps(describe(argv[2]), ensure_ascii=False, indent=1))
    elif cmd == "decode":
        print(json.dumps(decoded(argv[2]), ensure_ascii=False, indent=1))
    elif cmd == "dump":
        cmd_dump(argv[2])
    elif cmd == "diff" and len(argv) == 4:
        cmd_diff(argv[2], argv[3])
    elif cmd == "extract" and len(argv) == 4:
        cmd_extract(argv[2], argv[3])
    else:
        sys.exit(usage)


if __name__ == "__main__":
    main(sys.argv)
