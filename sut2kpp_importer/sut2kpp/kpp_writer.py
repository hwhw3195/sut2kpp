# Krita 5 paint-op preset (.kpp) writer.
#
# A .kpp is a PNG (the preset icon) with a zTXt "preset" chunk holding the XML and a tEXt "version" chunk.
# Krita 5 (KisPaintOpPreset::toXML) embeds linked resources as
#   <Preset ... embedded_resources="N"> <param .../>... <resources><resource type md5sum name filename>base64</...>
# where md5sum is the hex md5 of the resource file bytes.

import base64
import hashlib
import json
import struct
import zlib
from pathlib import Path
from xml.sax.saxutils import quoteattr

from . import tips as tipmod

TEMPLATES = Path(__file__).resolve().parent / "templates"


def template(paintop):
    return json.loads((TEMPLATES / f"{paintop}.json").read_text(encoding="utf-8"))


def md5_hex(data):
    return hashlib.md5(data).hexdigest()


class Preset:
    def __init__(self, name, paintop, params=None):
        self.name = name
        self.paintop = paintop
        self.params = template(paintop)
        self.params.update(params or {})
        self.params["paintop"] = paintop
        self.resources = []   # (type, filename, bytes)
        self.bundle_only = []  # (type, filename, bytes): resources for the .bundle, not embedded

    def embed(self, rtype, filename, data):
        self.resources.append((rtype, filename, data))
        return md5_hex(data)

    def xml(self):
        out = [f"<Preset paintopid={quoteattr(self.paintop)} name={quoteattr(self.name)} "
               f'embedded_resources="{len(self.resources)}">']
        for key in sorted(self.params):
            value = str(self.params[key])
            assert "]]>" not in value, key
            out.append(f" <param type=\"string\" name={quoteattr(key)}><![CDATA[{value}]]></param>")
        if self.resources:
            out.append(" <resources>")
            for rtype, filename, data in self.resources:
                name = filename.rsplit(".", 1)[0]
                out.append(f"  <resource type={quoteattr(rtype)} md5sum=\"{md5_hex(data)}\" name={quoteattr(name)} "
                           f"filename={quoteattr(filename)}><![CDATA[{base64.b64encode(data).decode('ascii')}]]>"
                           f"</resource>")
            out.append(" </resources>")
        out.append("</Preset>")
        return "\n".join(out)

    def kpp(self, icon_png=None):
        """The preset file: `icon_png` (any PNG) with the preset text chunks inserted after its IHDR."""
        image = icon_png or tipmod.png(1, 1, b"\xff\xff\xff\xff")
        ihdr_end = 8 + 25                   # signature + IHDR chunk; text chunks go right after it
        assert image[12:16] == b"IHDR"
        # iTXt, not zTXt: zTXt is Latin-1 by the PNG spec, so Qt would garble non-ASCII preset names.
        # iTXt layout: keyword \0, compression flag 1, method 0, language \0, translated keyword \0, zlib(UTF-8)
        text = (_chunk(b"iTXt", b"preset\0\x01\x00\0\0" + zlib.compress(self.xml().encode("utf-8"), 9))
                + _chunk(b"tEXt", b"version\x005.0"))
        return image[:ihdr_end] + text + image[ihdr_end:]


def _chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


# ---------------------------------------------------------------- reading back (tests, report)

def read_chunks(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    p, out = 8, []
    while p < len(data):
        n = struct.unpack(">I", data[p:p + 4])[0]
        tag, body = data[p + 4:p + 8], data[p + 8:p + 8 + n]
        crc = struct.unpack(">I", data[p + 8 + n:p + 12 + n])[0]
        out.append((tag, body, crc == zlib.crc32(tag + body)))
        p += 12 + n
    return out


def read_preset_xml(data):
    for tag, body, _ in read_chunks(data):
        if tag == b"zTXt" and body.startswith(b"preset\0"):
            return zlib.decompress(body[8:]).decode("latin-1")
        if tag == b"iTXt" and body.startswith(b"preset\0"):
            flag, rest = body[7], body[9:]
            rest = rest.split(b"\0", 2)[2]                  # skip language tag and translated keyword
            return (zlib.decompress(rest) if flag else rest).decode("utf-8")
    raise ValueError("no preset chunk")
