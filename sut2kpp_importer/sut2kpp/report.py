# <name>.report.md: what each CSP setting became in Krita, and what could not be carried over.

from . import dynamics as dyn

STATUS = {"exact": "✓", "approx": "≈", "dropped": "✗"}


def _pts(points):
    return " ".join(f"({x:g},{y:g})" for x, y in points) if points else "linear"


def write_report(path, b, conv, source):
    rep = conv.report
    lines = [f"# {b.name}", "",
             f"Source `{source}` (CSP format {b.version}) → Krita `{conv.preset.paintop}` preset.", "",
             "✓ exact · ≈ approximated · ✗ not convertible", "",
             "| | CSP | Krita | Note |", "|---|---|---|---|"]
    for csp, krita, status, note in rep.rows:
        lines.append(f"| {STATUS[status]} | {csp} | {krita} | {note} |")
    if rep.curves:
        lines += ["", "## Dynamics curves", "",
                  "CSP minimum is baked into the Krita curve: y' = min + (1 − min)·y. Tilt: y' = min + (max − min)·y, "
                  "resampled from CSP's 90° tilt axis onto Krita's 60° one.", "",
                  "| Krita option | Sensor | CSP curve | CSP min / max | Krita curve |", "|---|---|---|---|---|"]
        for option, sid, csp_pts, limits, curve in rep.curves:
            lines.append(f"| {option} | `{sid}` | {_pts(csp_pts)} | {limits[4:] if limits.startswith('min ') else limits} | `{curve}` |")
    if rep.notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in rep.notes]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
