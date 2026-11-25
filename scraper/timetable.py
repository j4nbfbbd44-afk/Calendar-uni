#!/usr/bin/env python3
"""Scrape the published university timetable (Google Sheets "publish to web")
and build per-group JSON + iCalendar feeds plus a static site.

Stdlib only. Usage:
    python3 scraper/timetable.py fetch   # download + parse, update data/weeks/
    python3 scraper/timetable.py build   # data/weeks/ + site/ -> _site/
    python3 scraper/timetable.py all     # both
    python3 scraper/timetable.py parse FILE.html "TAB NAME" "DOC TITLE"  # debug

What is deliberately NOT extracted: the rows below the schedule (student
rosters, advisor names, phone numbers, WhatsApp links) and the teacher / room
occupancy matrices to the right of the group columns.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "weeks"
SITE_SRC = ROOT / "site"
OUT = ROOT / "_site"

PUB_BASE = os.environ.get(
    "TIMETABLE_PUB_BASE",
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vRSeFfdQZEv5LGTfGPdmIm-c7Hfo4gV11NogYZsRxmqHVrt1MA24fLU_sGfg9kjAjyyOaYOYA_TO0mB",
)
TZID = "Asia/Almaty"  # Kazakhstan: single zone, UTC+5, no DST since 2024-03-01
UTC_OFFSET = dt.timedelta(hours=5)

# Sanity floor: if a parse yields fewer groups than this, treat it as broken
# and keep the last good data instead of publishing garbage.
MIN_GROUPS = 10

# The sheet is a repeating weekly timetable. Weeks after the latest recorded
# one are projected by copying it forward; a real recording replaces the
# projection as soon as it is fetched.
PROJECT_WEEKS = int(os.environ.get("TIMETABLE_PROJECT_WEEKS", "8"))

DAY_KEYS = [
    ("mon", ("пн", "mon")),
    ("tue", ("вт", "tue")),
    ("wed", ("ср", "wed")),
    ("thu", ("чт", "thu")),
    ("fri", ("пт", "fri")),
    ("sat", ("сб", "sat")),
    ("sun", ("вс", "sun")),
]
SLOT_RE = re.compile(r"^\s*(\d{1,2})[.:](\d{2})\s*[-–]\s*(\d{1,2})[.:](\d{2})\s*$")
INLINE_TIME_RE = re.compile(r"(\d{1,2})[.:](\d{2})\s*[-–]\s*(\d{1,2})[.:](\d{2})")
WEEK_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\s*[-–]\s*(\d{1,2})\.(\d{1,2})")


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
def http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "timetable-feed/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")


def list_tabs(index_html: str) -> list[dict]:
    """The pubhtml index page lists published tabs in inline JS."""
    tabs = []
    for m in re.finditer(r'items\.push\(\{name:\s*"((?:[^"\\]|\\.)*)".*?gid:\s*"(\d+)"', index_html):
        name = json.loads('"' + m.group(1) + '"')
        tabs.append({"name": name, "gid": m.group(2)})
    return tabs


def doc_title(index_html: str) -> str:
    m = re.search(r'<span class="name">(.*?)</span>', index_html, re.S)
    if m:
        return html.unescape(m.group(1)).strip()
    m = re.search(r"<title>(.*?)</title>", index_html, re.S)
    return html.unescape(m.group(1)).strip() if m else ""


# --------------------------------------------------------------------------
# HTML table -> merge-aware grid
# --------------------------------------------------------------------------
class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict]] = []
        self.cell: dict | None = None
        self.in_tbody = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tbody":
            self.in_tbody = True
        elif tag == "tr" and self.in_tbody:
            self.rows.append([])
        elif tag == "td" and self.in_tbody and self.rows:
            self.cell = {"t": "", "cs": int(a.get("colspan") or 1), "rs": int(a.get("rowspan") or 1)}
        elif tag == "br" and self.cell is not None:
            self.cell["t"] += "\n"

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.rows[-1].append(self.cell)
            self.cell = None
        elif tag == "tbody":
            self.in_tbody = False

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["t"] += data


class Grid:
    """cov[(r, c)] -> the cell object covering that position (merges expanded)."""

    def __init__(self, sheet_html: str):
        p = _TableParser()
        p.feed(sheet_html)
        self.cov: dict[tuple[int, int], dict] = {}
        self.nrows = len(p.rows)
        self.ncols = 0
        for r, row in enumerate(p.rows):
            c = 0
            for cell in row:
                while (r, c) in self.cov:
                    c += 1
                cell["t"] = re.sub(r"[ \t\xa0]+", " ", cell["t"]).strip()
                cell["r0"], cell["c0"] = r, c
                for dr in range(cell["rs"]):
                    for dc in range(cell["cs"]):
                        self.cov[(r + dr, c + dc)] = cell
                c += cell["cs"]
                self.ncols = max(self.ncols, c)

    def text(self, r, c) -> str:
        cell = self.cov.get((r, c))
        return cell["t"] if cell else ""


# --------------------------------------------------------------------------
# Parsing the timetable layout
# --------------------------------------------------------------------------
def day_index(label: str) -> int | None:
    low = label.lower()
    for i, (_, keys) in enumerate(DAY_KEYS):
        if any(re.search(r"(^|[^a-zа-яё])" + k, low) for k in keys):
            return i
    return None


def week_start(tab_name: str, title: str, today: dt.date | None = None) -> dt.date | None:
    m = WEEK_RE.search(tab_name)
    if not m:
        return None
    d, mth = int(m.group(1)), int(m.group(2))
    today = today or dt.date.today()
    ym = re.search(r"(20\d\d)\s*[-–/]\s*(20\d\d)", title)
    if ym:  # academic year "2026-2027": Aug..Dec -> first year, Jan..Jul -> second
        year = int(ym.group(1)) if mth >= 8 else int(ym.group(2))
    else:   # nearest year to today
        year = min((today.year - 1, today.year, today.year + 1),
                   key=lambda y: abs((dt.date(y, mth, d) - today).days))
    return dt.date(year, mth, d)


def slugify(label: str) -> str:
    tr = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
          "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
          "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
          "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya", "ә": "a", "ғ": "g", "қ": "q",
          "ң": "n", "ө": "o", "ұ": "u", "ү": "u", "һ": "h", "і": "i"}
    s = "".join(tr.get(ch, ch) for ch in label.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "group"


# Location extraction. Best effort: the full original cell text is always kept
# as the event title, so a miss here loses nothing.
_PLACES = [
    r"хим\.?\s*фак", r"chem\.?\s*faculty", r"biology faculty", r"юрфак", r"медфак", r"мехмат",
    r"восток", r"ффип", r"фмо малый зал", r"сим\s*центр", r"sim\.?\s*cent(?:er|re)",
    r"спорт\s*комплекс[а-я]*", r"sports? complex", r"медикер", r"нцпп", r"клиника", r"база",
    r"online", r"онлайн", r"moodle|мудл",
]
_PLACE_RE = re.compile("|".join(_PLACES), re.I)
_ROOMTOK = r"\d{1,4}(?:[-–]?[A-Za-zА-Яа-я0-9]{1,3})?(?:[-–]\d{1,4})?"


def extract_location(text: str) -> str:
    t = " ".join(text.split())
    t_wo_time = INLINE_TIME_RE.sub(" ", t)
    m = re.search(r"№\s*(" + _ROOMTOK + ")", t_wo_time)
    if m:
        room = "Room " + m.group(1)
        pm = _PLACE_RE.search(t_wo_time)
        return f"{room}, {pm.group(0)}" if pm else room
    m = _PLACE_RE.search(t_wo_time)
    if m:
        before = re.search(r"(" + _ROOMTOK + r")\s*$", t_wo_time[: m.start()])
        after = re.match(r"\s*(" + _ROOMTOK + r")\b", t_wo_time[m.end():])
        place = m.group(0)
        if after:
            return f"{place} {after.group(1)}"
        if before:
            return f"{place} {before.group(1)}"
        anywhere = re.search(r"(?<![\w.:])(\d{3}[A-Za-zА-Яа-я]?)(?![\w.:])", t_wo_time)
        if anywhere:  # "Analyt. Chem. 108 Abilev chem faculty"
            return f"{place} {anywhere.group(1)}"
        return place
    m = re.search(r"(" + _ROOMTOK + r")\s*ауд", t_wo_time, re.I)
    if m:
        return "Room " + m.group(1)
    m = re.search(r"\s(\d{1,4}[A-Za-zА-Яа-я]?)\s*$", t_wo_time)  # trailing "5A", "129"
    if m:
        return "Room " + m.group(1)
    return ""


def classify(text: str) -> str:
    low = text.lower()
    if re.search(r"\b(lecture|lec|лек|лекция)\b", low):
        return "lecture"
    if re.search(r"\b(sem|сем|seminar|семинар)\b", low):
        return "seminar"
    if re.search(r"physical|физ\.?\s*ра|физра|sports", low):
        return "pe"
    return ""


def parse_week(sheet_html: str, tab_name: str, title: str) -> dict:
    g = Grid(sheet_html)

    # Time columns: a column whose cells look like "8.00-9.00".
    time_cols = sorted({c for (r, c), cell in g.cov.items()
                        if cell["c0"] == c and cell["r0"] == r and SLOT_RE.match(cell["t"])})
    # keep columns with many slot labels (avoid stray matches)
    time_cols = [c for c in time_cols
                 if sum(1 for r in range(g.nrows) if SLOT_RE.match(g.text(r, c))) >= 6]
    if not time_cols:
        raise ValueError("no time columns found")

    # Schedule rows: rows where the first time column holds a slot label.
    tc0 = time_cols[0]
    slot_rows = [r for r in range(g.nrows) if SLOT_RE.match(g.text(r, tc0))]
    first_row, last_row = slot_rows[0], slot_rows[-1]
    warnings: list[str] = []

    # Header row with group names: the nearest row above first_row that has
    # non-empty text in most columns. Program titles sit one row above that.
    header_row = None
    for r in range(first_row - 1, -1, -1):
        if sum(1 for c in range(g.ncols) if g.text(r, c)) >= 5:
            header_row = r
            break
    if header_row is None:
        raise ValueError("no header row found")

    # Where the group area ends: the "TEACHERS" block (occupancy matrix).
    end_col = g.ncols
    for c in range(g.ncols):
        if g.text(header_row - 1, c).strip().upper() == "TEACHERS" or g.text(header_row, c).strip().upper() == "TEACHERS":
            end_col = c
            break
    time_cols = [c for c in time_cols if c < end_col]

    def time_col_for(c):
        left = [tc for tc in time_cols if tc < c]
        return left[-1] if left else None

    day_cols = {tc: tc - 1 for tc in time_cols}

    # Layout checks: every block should label the same rows with slots and days.
    for tc in time_cols:
        rows = [r for r in range(first_row, last_row + 1) if SLOT_RE.match(g.text(r, tc))]
        if rows != slot_rows:
            warnings.append(f"Layout: time column {tc} labels {len(rows)} slots, first block labels {len(slot_rows)}")
        bad_days = {g.text(r, tc - 1) for r in rows if day_index(g.text(r, tc - 1)) is None}
        if bad_days:
            warnings.append(f"Layout: unrecognised day label(s) {sorted(bad_days)!r} next to time column {tc}")

    groups = {}
    used_slugs = set()
    for c in range(end_col):
        name = g.text(header_row, c)
        cell = g.cov.get((header_row, c))
        if not name or c in time_cols or c in day_cols.values() or cell["c0"] != c or cell["cs"] != 1:
            continue
        tc = time_col_for(c)
        if tc is None or not re.search(r"\d", name):
            continue
        program = g.text(header_row - 1, c)
        if not program:  # program title cell may start further left
            for cc in range(c, -1, -1):
                if g.text(header_row - 1, cc):
                    program = g.text(header_row - 1, cc)
                    break
        slug = slugify(name)
        base, n = slug, 2
        while slug in used_slugs:
            slug, n = f"{base}-{n}", n + 1
        used_slugs.add(slug)
        if slug != base:
            warnings.append(f"Duplicate group name {name!r}; feed published as {slug}")
        groups[slug] = {"slug": slug, "name": name, "program": program, "col": c, "tc": tc}

    events_by_group: dict[str, list] = {}
    notes_by_group: dict[str, list] = {}
    for slug, info in groups.items():
        c, tc = info["col"], info["tc"]
        dcol = day_cols[tc]
        evs = []
        cur = None  # (day, cellobj, start, end)
        for r in range(first_row, last_row + 1):
            m = SLOT_RE.match(g.text(r, tc))
            cell = g.cov.get((r, c))
            txt = cell["t"] if cell else ""
            if not m:
                if txt:
                    warnings.append(f"{info['name']}: skipped {txt!r} (row {r + 1} has no time label)")
                cur = _flush(cur, evs)
                continue
            di = day_index(g.text(r, dcol))
            if di is None or not txt:
                if txt:
                    warnings.append(f"{info['name']}: skipped {txt!r} (row {r + 1} has no recognisable day)")
                cur = _flush(cur, evs)
                continue
            start = (int(m.group(1)), int(m.group(2)))
            end = (int(m.group(3)), int(m.group(4)))
            if cur and cur["day"] == di and cur["text"] == txt and cur["end"] == start:
                cur["end"] = end
                cur["cells"].add(id(cell))
            else:
                cur = _flush(cur, evs)
                cur = {"day": di, "text": txt, "start": start, "end": end, "cells": {id(cell)},
                       "shared_cols": _cell_cols(cell)}
        _flush(cur, evs)
        events_by_group[slug] = evs

        # Notes: non-numeric text in the gap rows right after the schedule and
        # before the next labelled row (e.g. "Иностр яз. СДО МУДЛ - онлайн").
        notes = []
        for r in range(last_row + 1, min(last_row + 4, g.nrows)):
            if any(g.text(r, t) for t in time_cols):
                break
            t = g.text(r, c)
            if t and not re.fullmatch(r"[\d\s]+", t):
                notes.append(t)
        notes_by_group[slug] = notes

    col_to_slug = {info["col"]: s for s, info in groups.items()}
    ws = week_start(tab_name, title)
    out_groups = []
    for slug, info in groups.items():
        evs = []
        for e in events_by_group[slug]:
            text = e["text"]
            sh, sm, eh, em = *e["start"], *e["end"]
            m = INLINE_TIME_RE.search(text)
            if m:  # explicit time inside the cell overrides the grid slots
                ish, ism, ieh, iem = (int(x) for x in m.groups())
                grid_s, grid_e = sh * 60 + sm, eh * 60 + em
                in_s, in_e = ish * 60 + ism, ieh * 60 + iem
                day = DAY_KEYS[e["day"]][0].title()
                if not (6 * 60 <= in_s < in_e <= 22 * 60):
                    warnings.append(f"{info['name']}: {day} {text!r} has an impossible time; using grid slots "
                                    f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}")
                else:
                    if abs(in_s - grid_s) > 90 or abs(in_e - grid_e) > 90:
                        warnings.append(f"{info['name']}: {day} {text!r} written time differs from its grid "
                                        f"slots ({sh:02d}:{sm:02d}-{eh:02d}:{em:02d}) by more than 90 min; "
                                        f"using the written time")
                    sh, sm, eh, em = ish, ism, ieh, iem
            shared = [groups[col_to_slug[cc]]["name"] for cc in e["shared_cols"]
                      if cc in col_to_slug and col_to_slug[cc] != slug]
            evs.append({
                "day": e["day"],
                "start": f"{sh:02d}:{sm:02d}",
                "end": f"{eh:02d}:{em:02d}",
                "text": text.replace("\n", " "),
                "location": extract_location(text),
                "kind": classify(text),
                "shared_with": shared,
            })
        ordered = sorted(evs, key=lambda e: (e["day"], e["start"]))
        for a, b in zip(ordered, ordered[1:]):
            if a["day"] == b["day"] and b["start"] < a["end"]:
                warnings.append(f"{info['name']}: overlapping classes on {DAY_KEYS[a['day']][0].title()}: "
                                f"{a['start']}-{a['end']} {a['text']!r} and {b['start']}-{b['end']} {b['text']!r}")
        for e in evs:
            if not e["location"]:
                warnings.append(f"{info['name']}: no room found in {e['text']!r}")
        out_groups.append({"slug": slug, "name": info["name"], "program": info["program"],
                           "events": evs, "notes": notes_by_group[slug]})
    return {"tab": tab_name, "title": title, "week_start": ws.isoformat() if ws else None,
            "groups": out_groups, "warnings": sorted(set(warnings))}


def _cell_cols(cell):
    return list(range(cell["c0"], cell["c0"] + cell["cs"])) if cell else []


def _flush(cur, evs):
    if cur:
        evs.append(cur)
    return None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
REPORT = ROOT / "_report" / "report.json"
CHANGES = ROOT / "data" / "changes.json"
MAX_CHANGES = 1000


def _ev_key(e):
    return (e["day"], e["start"], e["end"], e["text"])


def _fmt_ev(e):
    return f"{DAY_KEYS[e['day']][0].title()} {e['start']}-{e['end']} {e['text']}"


def diff_weeks(old: dict, new: dict) -> list[dict]:
    """Per-group differences between two parsed weeks (events compared by day/time/text)."""
    out = []
    old_g = {g["slug"]: g for g in old["groups"]}
    new_g = {g["slug"]: g for g in new["groups"]}
    for slug in sorted(set(old_g) - set(new_g)):
        out.append({"group": slug, "name": old_g[slug]["name"], "kind": "group_removed",
                    "text": f"Group {old_g[slug]['name']!r} is no longer in the sheet"})
    for slug in sorted(set(new_g) - set(old_g)):
        out.append({"group": slug, "name": new_g[slug]["name"], "kind": "group_added",
                    "text": f"New group {new_g[slug]['name']!r}"})
    for slug in sorted(set(old_g) & set(new_g)):
        a = {_ev_key(e): e for e in old_g[slug]["events"]}
        b = {_ev_key(e): e for e in new_g[slug]["events"]}
        removed = [a[k] for k in a if k not in b]
        added = [b[k] for k in b if k not in a]
        # Pair removed/added at the same day+start as a change.
        for r in list(removed):
            match = next((x for x in added if x["day"] == r["day"] and x["start"] == r["start"]), None)
            if match:
                removed.remove(r)
                added.remove(match)
                out.append({"group": slug, "name": new_g[slug]["name"], "kind": "changed",
                            "text": f"{_fmt_ev(r)}  ->  {match['start']}-{match['end']} {match['text']}"})
        for r in removed:
            out.append({"group": slug, "name": new_g[slug]["name"], "kind": "removed", "text": _fmt_ev(r)})
        for x in added:
            out.append({"group": slug, "name": new_g[slug]["name"], "kind": "added", "text": _fmt_ev(x)})
    return out


def _monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def cmd_fetch() -> int:
    """Fetch + parse. Always writes _report/report.json describing what happened."""
    DATA.mkdir(parents=True, exist_ok=True)
    report = {"errors": [], "warnings": [], "changes": [], "weeks_updated": []}
    try:
        rc = _fetch(report)
    except Exception as e:  # network errors etc.
        report["errors"].append(f"Fetch crashed: {type(e).__name__}: {e}")
        rc = 1

    # Stale sheet: by Monday midday (UTC+5) the current week should be published.
    now_local = dt.datetime.now(dt.timezone.utc) + UTC_OFFSET
    weeks = sorted(p.stem for p in DATA.glob("*.json"))
    this_monday = _monday(now_local.date())
    if weeks and dt.date.fromisoformat(weeks[-1]) < this_monday and not (
            now_local.weekday() == 0 and now_local.hour < 12):
        report["warnings"].append(
            f"No timetable recorded for the week of {this_monday.isoformat()} yet (latest is {weeks[-1]}). "
            "The new week's tab may not be published; feeds show a projection of the old week.")

    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    for k in ("errors", "warnings"):
        for line in report[k]:
            print(f"{k[:-1].upper()}: {line}", file=sys.stderr)
    print(f"{len(report['changes'])} change(s)")
    return rc


def _fetch(report: dict) -> int:
    index = http_get(f"{PUB_BASE}/pubhtml")
    title = doc_title(index)
    tabs = list_tabs(index)
    if not tabs:
        report["errors"].append("No published tabs found on the sheet's publish page")
        return 1
    ok = 0
    for tab in tabs:
        if not WEEK_RE.search(tab["name"]):
            report["warnings"].append(f"Ignored published tab without a week range: {tab['name']!r}")
            continue
        sheet = http_get(f"{PUB_BASE}/pubhtml/sheet?headers=false&gid={tab['gid']}")
        try:
            week = parse_week(sheet, tab["name"], title)
        except Exception as e:  # keep last good data
            report["errors"].append(f"Parse failed for tab {tab['name']!r}: {e}. Keeping last good data.")
            continue
        warnings = week.pop("warnings", [])
        report["warnings"] += [f"[{tab['name']}] {w}" for w in warnings]
        n_ev = sum(len(g["events"]) for g in week["groups"])
        if len(week["groups"]) < MIN_GROUPS or n_ev == 0 or not week["week_start"]:
            report["errors"].append(
                f"Suspicious parse for tab {tab['name']!r}: {len(week['groups'])} groups, {n_ev} events, "
                f"week start {week['week_start']}. Not saved; keeping last good data.")
            continue
        path = DATA / f"{week['week_start']}.json"
        new = json.dumps(week, ensure_ascii=False, indent=1, sort_keys=True)
        old = path.read_text(encoding="utf-8") if path.exists() else None
        ok += 1
        if old == new:
            print(f"unchanged {path.name}")
            continue
        # What do we compare against? The same week if we had it, else the latest
        # earlier week (what the feeds were projecting).
        if old is not None:
            base, why = json.loads(old), "updated"
        else:
            prev = sorted(p for p in DATA.glob("*.json") if p.stem < week["week_start"])
            base = json.loads(prev[-1].read_text(encoding="utf-8")) if prev else None
            why = f"new week vs {prev[-1].stem}" if prev else "first week"
        changes = diff_weeks(base, week) if base else []
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        for c in changes:
            c.update({"week": week["week_start"], "at": stamp, "why": why})
        report["changes"] += changes
        report["weeks_updated"].append(week["week_start"])
        path.write_text(new, encoding="utf-8")
        print(f"{why}: {path.name}: {len(week['groups'])} groups, {n_ev} events, {len(changes)} changes")
    if report["changes"]:
        log = json.loads(CHANGES.read_text(encoding="utf-8")) if CHANGES.exists() else []
        log = (log + report["changes"])[-MAX_CHANGES:]
        CHANGES.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if ok else 1


def ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_fold(line: str) -> str:
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > 73:
            out.append(cur.decode("utf-8"))
            cur = b" " + b
        else:
            cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n".join(out)


def build_ics(name: str, slug: str, events: list[dict]) -> str:
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//timetable-feed//EN", "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH", f"X-WR-CALNAME:{ics_escape(name)}", f"X-WR-TIMEZONE:{TZID}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT2H", "X-PUBLISHED-TTL:PT2H",
        "BEGIN:VTIMEZONE", f"TZID:{TZID}", "BEGIN:STANDARD", "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0500", "TZOFFSETTO:+0500", "TZNAME:+05", "END:STANDARD", "END:VTIMEZONE",
    ]
    for e in events:
        d = e["date"].replace("-", "")
        st, en = e["start"].replace(":", "") + "00", e["end"].replace(":", "") + "00"
        uid_src = f"{slug}|{e['date']}|{e['start']}|{e['end']}|{e['text']}"
        uid = hashlib.sha1(uid_src.encode()).hexdigest()[:20] + "@timetable-feed"
        desc = e["text"]
        if e["shared_with"]:
            desc += "\nShared with: " + ", ".join(e["shared_with"])
        if e.get("projected"):
            desc += "\nProjected from the current weekly timetable; may change."
        desc += "\nUnofficial feed - check the official timetable if in doubt."
        lines += [
            "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{e['stamp']}",
            f"DTSTART;TZID={TZID}:{d}T{st}", f"DTEND;TZID={TZID}:{d}T{en}",
            f"SUMMARY:{ics_escape(e['text'])}",
        ]
        if e["location"]:
            lines.append(f"LOCATION:{ics_escape(e['location'])}")
        lines += [f"DESCRIPTION:{ics_escape(desc)}", "TRANSP:OPAQUE", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(l) for l in lines) + "\r\n"


def cmd_build() -> int:
    import shutil
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(SITE_SRC, OUT)
    (OUT / "data").mkdir()
    (OUT / "ics").mkdir()

    weeks = []
    for p in sorted(DATA.glob("*.json")):
        weeks.append(json.loads(p.read_text(encoding="utf-8")))
    if not weeks:
        print("no week data", file=sys.stderr)
        return 1

    groups: dict[str, dict] = {}  # slug -> {name, program, weeks:{ws:{events,notes}}}
    for w in weeks:
        ws = dt.date.fromisoformat(w["week_start"])
        stamp = dt.datetime.fromtimestamp((DATA / f"{w['week_start']}.json").stat().st_mtime,
                                          dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for g in w["groups"]:
            entry = groups.setdefault(g["slug"], {"slug": g["slug"], "name": g["name"],
                                                  "program": g["program"], "weeks": {}})
            entry["name"], entry["program"] = g["name"], g["program"]  # latest wins
            evs = []
            for e in g["events"]:
                e2 = dict(e)
                e2["date"] = (ws + dt.timedelta(days=e["day"])).isoformat()
                e2["stamp"] = stamp
                evs.append(e2)
            entry["weeks"][w["week_start"]] = {"events": evs, "notes": g.get("notes", []), "projected": False}

    # Project the latest recorded week forward.
    latest = weeks[-1]
    latest_ws = dt.date.fromisoformat(latest["week_start"])
    latest_slugs = {g["slug"] for g in latest["groups"]}
    projected_keys = []
    for k in range(1, PROJECT_WEEKS + 1):
        ws = latest_ws + dt.timedelta(weeks=k)
        key = ws.isoformat()
        projected_keys.append(key)
        for slug in latest_slugs:
            src = groups[slug]["weeks"][latest["week_start"]]
            evs = []
            for e in src["events"]:
                e2 = dict(e)
                e2["date"] = (ws + dt.timedelta(days=e["day"])).isoformat()
                e2["projected"] = True
                evs.append(e2)
            groups[slug]["weeks"][key] = {"events": evs, "notes": src["notes"], "projected": True}

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    index = {"generated": now, "tz": TZID, "utc_offset_minutes": int(UTC_OFFSET.total_seconds() // 60),
             "weeks": [w["week_start"] for w in weeks] + projected_keys,
             "latest_recorded": latest["week_start"],
             "source": f"{PUB_BASE}/pubhtml",
             "groups": [{"slug": g["slug"], "name": g["name"], "program": g["program"]}
                        for g in sorted(groups.values(), key=lambda g: (g["program"], g["name"]))]}
    (OUT / "data" / "index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    log = json.loads(CHANGES.read_text(encoding="utf-8")) if CHANGES.exists() else []
    for slug, g in groups.items():
        g_changes = [{k: c[k] for k in ("at", "week", "kind", "text")} for c in log if c["group"] == slug][-30:]
        weeks_out = {ws: {"events": [{k: v for k, v in e.items() if k != "stamp"} for e in wk["events"]],
                          "notes": wk["notes"], "projected": wk["projected"]}
                     for ws, wk in sorted(g["weeks"].items())}
        (OUT / "data" / f"{slug}.json").write_text(
            json.dumps({"slug": slug, "name": g["name"], "program": g["program"], "weeks": weeks_out,
                        "changes": g_changes},
                       ensure_ascii=False), encoding="utf-8")
        all_events = [e for wk in g["weeks"].values() for e in wk["events"]]
        (OUT / "ics" / f"{slug}.ics").write_text(build_ics(g["name"], slug, all_events),
                                                 encoding="utf-8", newline="")
    (OUT / ".nojekyll").write_text("")
    print(f"built {_rel(OUT)}: {len(groups)} groups, {len(weeks)} weeks")
    return 0


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "all"
    if cmd == "fetch":
        return cmd_fetch()
    if cmd == "build":
        return cmd_build()
    if cmd == "all":
        rc = cmd_fetch()
        if rc:
            print("fetch failed; building from last good data", file=sys.stderr)
        return cmd_build()
    if cmd == "parse":
        sheet = Path(argv[2]).read_text(encoding="utf-8")
        print(json.dumps(parse_week(sheet, argv[3], argv[4] if len(argv) > 4 else ""),
                         ensure_ascii=False, indent=1))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
