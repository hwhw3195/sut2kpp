# Krita resource bundle (.bundle): a zip with mimetype, meta.xml, META-INF/manifest.xml and resource folders.
# Tips are added as brushes/ entries too, so they exist even if a Krita version ignores embedded resources.

import hashlib
import zipfile
from datetime import date
from xml.sax.saxutils import escape, quoteattr

from . import tips as tipmod


def write_bundle(path, entries, title=None, tags=()):
    """entries: [(resource type, filename, bytes)], e.g. ("paintoppresets", "Brush.kpp", data).
    `tags` are attached to every preset through the manifest, the way Krita's own bundles tag them."""
    title = title or path.stem
    seen, manifest = set(), []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/x-krita-resourcebundle", zipfile.ZIP_STORED)
        z.writestr("meta.xml", f"""<?xml version="1.0" encoding="UTF-8"?>
<meta:meta xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" xmlns:dc="http://purl.org/dc/elements/1.1">
 <meta:generator>sut2kpp</meta:generator>
 <meta:bundle-version>1</meta:bundle-version>
 <dc:title>{escape(title)}</dc:title>
 <dc:description>Converted from Clip Studio Paint sub tools by sut2kpp</dc:description>
 <meta:creation-date>{date.today().isoformat()}</meta:creation-date>
 <meta:dc-date>{date.today().isoformat()}</meta:dc-date>
</meta:meta>
""")
        z.writestr("preview.png", tipmod.png(1, 1, b"\xff\xff\xff\xff"))
        for rtype, filename, data in entries:
            full = f"{rtype}/{filename}"
            if full in seen:
                continue
            seen.add(full)
            z.writestr(full, data)
            entry = (f' <manifest:file-entry manifest:media-type="{rtype}" manifest:full-path={quoteattr(full)} '
                     f'manifest:md5sum="{hashlib.md5(data).hexdigest()}"')
            if tags and rtype == "paintoppresets":
                entry += (">\n  <manifest:tags>\n"
                          + "".join(f"   <manifest:tag>{escape(t)}</manifest:tag>\n" for t in tags)
                          + "  </manifest:tags>\n </manifest:file-entry>")
            else:
                entry += "/>"
            manifest.append(entry)
        z.writestr("META-INF/manifest.xml",
                   '<?xml version="1.0" encoding="UTF-8"?>\n'
                   '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
                   'manifest:version="1.2">\n'
                   ' <manifest:file-entry manifest:media-type="application/x-krita-resourcebundle" '
                   'manifest:full-path="/"/>\n' + "\n".join(manifest) + "\n</manifest:manifest>\n")
