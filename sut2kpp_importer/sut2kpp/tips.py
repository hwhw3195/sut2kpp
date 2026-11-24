# CSP brush tips -> Krita brush files: a .png for one tip, a .gih (GIMP image pipe of GBR cells) for several.
#
# CSP scales every tip of a multi-tip brush to the brush size on its own, while Krita applies one scale to all
# cells of a .gih. So each tip is resized here so its longer side is the same, then centred on a shared canvas.

import struct
import zlib

from .csp_model import Tip

# BrushPatternOrderType2 -> (GIH selection mode, ping-pong cells, exact?)
REPEAT_MODES = {0: ("incremental", False, True), 1: ("incremental", True, True), 2: ("incremental", False, False),
                3: ("random", False, True), 4: ("incremental", False, False), 5: ("random", False, False)}


def is_color(tip):
    """True if any visible pixel is not gray: such tips keep their colours (Krita image stamp)."""
    px = tip.rgba
    for i in range(0, len(px), 4 * 7):          # sampling every 7th pixel is plenty to spot colour
        if px[i + 3] and not (px[i] == px[i + 1] == px[i + 2]):
            return True
    return False


def coverage(tip):
    """8-bit paint coverage of a gray tip: CSP paints where the material is dark and opaque."""
    px = tip.rgba
    a, g = px[3::4], px[0::4]
    return bytes((ai * (255 - gi) + 127) // 255 for ai, gi in zip(a, g))


def resize(tip, longest):
    """Area-average downscale (never upscales) so the longer side is `longest` px."""
    w, h = tip.width, tip.height
    s = longest / max(w, h)
    if s >= 1:
        return tip
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    src = tip.rgba
    # premultiply so transparent pixels don't bleed their (meaningless) colour into the edges
    out = bytearray(nw * nh * 4)
    xs = [(int(x * w / nw), max(int(x * w / nw) + 1, int((x + 1) * w / nw))) for x in range(nw)]
    for y in range(nh):
        y0, y1 = int(y * h / nh), max(int(y * h / nh) + 1, int((y + 1) * h / nh))
        rows = [src[r * w * 4:(r + 1) * w * 4] for r in range(y0, y1)]
        for x, (x0, x1) in enumerate(xs):
            r = g = b = a = 0
            for row in rows:
                seg = row[x0 * 4:x1 * 4]
                for i in range(0, len(seg), 4):
                    pa = seg[i + 3]
                    r += seg[i] * pa
                    g += seg[i + 1] * pa
                    b += seg[i + 2] * pa
                    a += pa
            n = (y1 - y0) * (x1 - x0)
            o = (y * nw + x) * 4
            if a:
                out[o:o + 4] = bytes((r // a, g // a, b // a, a // n))
    return Tip(tip.name, nw, nh, bytes(out))


def fit(tip, longest):
    """Scale to exactly `longest` on the longer side: area-average down, nearest-neighbour up (icons only)."""
    if max(tip.width, tip.height) >= longest:
        return resize(tip, longest)
    s = longest / max(tip.width, tip.height)
    nw, nh = max(1, round(tip.width * s)), max(1, round(tip.height * s))
    src, out = tip.rgba, bytearray(nw * nh * 4)
    for y in range(nh):
        sy = min(int(y / s), tip.height - 1)
        for x in range(nw):
            o, i = (y * nw + x) * 4, (sy * tip.width + min(int(x / s), tip.width - 1)) * 4
            out[o:o + 4] = src[i:i + 4]
    return Tip(tip.name, nw, nh, bytes(out))


def pad(tip, w, h):
    """Centre on a transparent w x h canvas (white, so gray tips stay neutral)."""
    if (tip.width, tip.height) == (w, h):
        return tip
    out = bytearray(b"\xff\xff\xff\x00" * (w * h))
    ox, oy = (w - tip.width) // 2, (h - tip.height) // 2
    row = tip.width * 4
    for y in range(tip.height):
        o = ((oy + y) * w + ox) * 4
        out[o:o + row] = tip.rgba[y * row:(y + 1) * row]
    return Tip(tip.name, w, h, bytes(out))


def normalise(tips, longest):
    """Resize each tip to the same longer side, then pad all to one canvas. Returns (tips, width, height)."""
    tips = [resize(t, longest) for t in tips]
    w, h = max(t.width for t in tips), max(t.height for t in tips)
    return [pad(t, w, h) for t in tips], w, h


def mask_rgba(tip):
    """Black paint with coverage as alpha: the unambiguous way to hand Krita a mask tip."""
    cov = coverage(tip)
    out = bytearray(len(cov) * 4)
    out[3::4] = cov
    return bytes(out)


# ---------------------------------------------------------------- file writers

def png(w, h, rgba, text=None):
    """RGBA PNG; `text` (key -> str) goes into uncompressed UTF-8 iTXt chunks."""
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    # iTXt: keyword \0, compression flag 0, method 0, language \0, translated keyword \0, UTF-8 text
    texts = b"".join(chunk(b"iTXt", k.encode("latin-1") + b"\0\0\0\0\0" + v.encode("utf-8"))
                     for k, v in (text or {}).items())
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) + texts
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def gbr(name, w, h, data, bpp, spacing=25):
    """GIMP brush v2. bpp 1 = gray (255 = full paint), 4 = RGBA."""
    nm = name.encode("utf-8") + b"\0"
    return struct.pack(">7I", 28 + len(nm), 2, w, h, bpp, 0x47494D50, spacing) + nm + data


def gih(name, tips, selection, color):
    """GIMP image pipe: name line, parameter line, then one GBR per cell. All tips must share one size."""
    w, h = tips[0].width, tips[0].height
    n = len(tips)
    params = (f"{n} ncells:{n} cellwidth:{w} cellheight:{h} step:100 dim:1 cols:1 rows:1 "
              f"placement:constant rank0:{n} sel0:{selection}")
    out = [name.encode("utf-8") + b"\n", params.encode("ascii") + b"\n"]
    for i, t in enumerate(tips):
        data = t.rgba if color else coverage(t)
        out.append(gbr(f"{name} {i + 1}", w, h, data, 4 if color else 1))
    return b"".join(out)


def ping_pong(tips):
    return tips + tips[-2:0:-1] if len(tips) > 2 else tips


def parse_gih(data):
    """Minimal reader used by the tests: returns (name, params dict, [(w, h, bpp), ...])."""
    first = data.index(b"\n")
    second = data.index(b"\n", first + 1)
    name = data[:first].decode("utf-8")
    fields = data[first + 1:second].decode("ascii").split()
    params = dict(f.split(":", 1) for f in fields[1:])
    cells, p = [], second + 1
    for _ in range(int(fields[0])):
        hs, ver, w, h, bpp, magic, _sp = struct.unpack_from(">7I", data, p)
        assert ver == 2 and magic == 0x47494D50
        cells.append((w, h, bpp))
        p += hs + w * h * bpp
    assert p == len(data)
    return name, params, cells
