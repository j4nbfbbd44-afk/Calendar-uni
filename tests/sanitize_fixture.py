#!/usr/bin/env python3
"""Make a test fixture from a downloaded week sheet, keeping ONLY the schedule grid.

Drops everything below the schedule (student lists, advisor names, phone numbers,
chat links) and everything from the TEACHERS column rightwards.

    python3 tests/sanitize_fixture.py SHEET.html tests/fixtures/OUT.html
"""
import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))
import timetable as tt  # noqa: E402

src, dst = sys.argv[1], sys.argv[2]
g = tt.Grid(Path(src).read_text(encoding="utf-8"))
tc = next(c for c in range(g.ncols) if any(tt.SLOT_RE.match(g.text(r, c)) for r in range(g.nrows)))
slot_rows = [r for r in range(g.nrows) if tt.SLOT_RE.match(g.text(r, tc))]
last_row = slot_rows[-1] + 2          # keep the per-group note rows right under the grid
end_col = next((c for c in range(g.ncols) if any(g.text(r, c).upper() == "TEACHERS" for r in range(4))), g.ncols)

out = ["<table><tbody>"]
for r in range(last_row + 1):
    out.append("<tr>")
    for c in range(end_col):
        cell = g.cov.get((r, c))
        if cell is None:
            out.append("<td></td>")
            continue
        if cell["r0"] != r or cell["c0"] != c:
            continue
        rs = min(cell["rs"], last_row + 1 - r)
        cs = min(cell["cs"], end_col - c)
        t = cell["t"]
        if re.search(r"\+?\d[\d\s()-]{8,}\d", t) and not tt.SLOT_RE.match(t):
            t = ""  # paranoia: drop anything phone-like
        attrs = (f' rowspan="{rs}"' if rs > 1 else "") + (f' colspan="{cs}"' if cs > 1 else "")
        out.append(f"<td{attrs}>{html.escape(t).replace(chr(10), '<br>')}</td>")
    out.append("</tr>")
out.append("</tbody></table>")
Path(dst).write_text("".join(out), encoding="utf-8")
print(f"wrote {dst}: rows 0..{last_row}, cols 0..{end_col - 1}")
