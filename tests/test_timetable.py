"""Regression tests on a sanitized copy of the real sheet (week of 2026-11-23).

Run: python3 -m unittest discover -s tests
"""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scraper"))
import notify  # noqa: E402
import timetable as tt  # noqa: E402

FIXTURE = (HERE / "fixtures" / "week-2026-11-23.html").read_text(encoding="utf-8")
TAB, TITLE = "🔴23.11-29.11", "Расписание занятий 2026-2027 _Timetable of classes 2026-2027"
PE = "Physical education in sports complex 16:30-18:30"


def parse(html=FIXTURE):
    return tt.parse_week(html, TAB, TITLE)


def group(week, slug):
    return next(g for g in week["groups"] if g["slug"] == slug)


class RealLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.week = parse()

    def test_shape(self):
        self.assertEqual(self.week["week_start"], "2026-11-23")
        self.assertEqual(len(self.week["groups"]), 72)
        self.assertEqual(sum(len(g["events"]) for g in self.week["groups"]), 625)
        self.assertEqual(self.week["warnings"], [])

    def test_m_26_04_week(self):
        got = [(e["day"], e["start"], e["end"], e["location"]) for e in group(self.week, "m-26-04-eng-october")["events"]]
        self.assertEqual(got, [
            (0, "08:00", "11:00", "Room 307"),
            (0, "16:30", "18:30", "sports complex"),
            (1, "13:00", "15:00", "Room 313"),
            (2, "14:00", "15:00", "Room 105"),
            (2, "15:00", "18:00", "Room 106"),
            (4, "11:00", "14:00", "Room 313"),
            (5, "12:00", "15:00", "Room 106"),
            (5, "15:00", "18:00", "Room 314"),
        ])

    def test_shared_pe_cell(self):
        """PE is one cell merged across 9 groups, with its own time written inside."""
        pe = [e for e in group(self.week, "m-26-04-eng-october")["events"] if e["kind"] == "pe"]
        self.assertEqual(len(pe), 1)
        self.assertEqual((pe[0]["start"], pe[0]["end"]), ("16:30", "18:30"))
        self.assertEqual(len(pe[0]["shared_with"]), 8)
        for name in pe[0]["shared_with"] + ["M 26-04 eng october"]:
            g = next(g for g in self.week["groups"] if g["name"] == name)
            self.assertIn(PE, [e["text"] for e in g["events"]], name)

    def test_every_event_has_location(self):
        for g in self.week["groups"]:
            for e in g["events"]:
                self.assertTrue(e["location"], (g["name"], e["text"]))

    def test_notes_row(self):
        self.assertEqual(group(self.week, "m-26-01-kaz")["notes"], ["Иностр яз. СДО МУДЛ - онлайн"])


class ChecksFire(unittest.TestCase):
    def test_impossible_inline_time(self):
        w = parse(FIXTURE.replace(PE, "Physical education in sports complex 16:30-02:30"))
        self.assertTrue(any("impossible time" in x for x in w["warnings"]), w["warnings"])
        pe = [e for e in group(w, "m-26-04-eng-october")["events"] if e["kind"] == "pe"][0]
        self.assertEqual((pe["start"], pe["end"]), ("16:00", "18:00"))  # falls back to grid slots

    def test_far_inline_time_and_overlap(self):
        w = parse(FIXTURE.replace(PE, "Physical education in sports complex 09:30-10:30"))
        self.assertTrue(any("differs from its grid" in x for x in w["warnings"]), w["warnings"])
        self.assertTrue(any("M 26-04 eng october: overlapping" in x for x in w["warnings"]), w["warnings"])

    def test_unknown_day_label(self):
        w = parse(FIXTURE.replace("чт \\ thus", "XX"))
        self.assertTrue(any("unrecognised day" in x for x in w["warnings"]), w["warnings"])
        self.assertTrue(any("skipped" in x for x in w["warnings"]), w["warnings"])

    def test_missing_room(self):
        w = parse(FIXTURE.replace("Biology sem №307 Alibekova", "Biology sem Alibekova"))
        self.assertTrue(any("no room found in 'Biology sem Alibekova'" in x for x in w["warnings"]))

    def test_broken_layout_raises(self):
        with self.assertRaises(ValueError):
            parse("<table><tbody><tr><td>nothing here</td></tr></tbody></table>")


class Diff(unittest.TestCase):
    def test_room_change_move_and_removal(self):
        a = parse()
        b = parse(FIXTURE.replace("Biology sem №307 Alibekova", "Biology sem №999 Alibekova")
                  .replace("Қазақ тілі Моминов Н. 105 ауд", ""))
        ch = [c for c in tt.diff_weeks(a, b) if c["group"] == "m-26-04-eng-october"]
        kinds = sorted(c["kind"] for c in ch)
        self.assertEqual(kinds, ["changed", "removed"], ch)
        self.assertIn("№999", next(c for c in ch if c["kind"] == "changed")["text"])

    def test_no_changes(self):
        self.assertEqual(tt.diff_weeks(parse(), parse()), [])


class Helpers(unittest.TestCase):
    def test_week_year(self):
        self.assertEqual(tt.week_start("29.12-04.01", TITLE), dt.date(2026, 12, 29))
        self.assertEqual(tt.week_start("05.01-11.01", TITLE), dt.date(2027, 1, 5))

    def test_locations(self):
        cases = {
            "Analyt. Chem. 108 Abilev chem faculty": "chem faculty 108",
            "Аналит. химия Джуманова 107 химфак": "химфак 107",
            "Қазақ тілі Моминов Н. 105 ауд": "Room 105",
            "физ.ра спорт комплекс 16:20-18:20": "спорт комплекс",
            "Stat. Meth. Pharm. Saktapov 5A": "Room 5A",
            "Pathology lecture Mussazhanova Online": "Online",
        }
        for text, want in cases.items():
            self.assertEqual(tt.extract_location(text), want, text)

    def test_ics_lines(self):
        ev = [{"date": "2026-11-23", "start": "16:30", "end": "18:30", "text": PE, "location": "x, y; z",
               "shared_with": ["A"] * 30, "stamp": "20261125T000000Z"}]
        ics = tt.build_ics("M 26-04", "m", ev)
        self.assertTrue(ics.endswith("\r\n"))
        for line in ics.split("\r\n"):
            self.assertLessEqual(len(line.encode()), 75, line)
        self.assertIn("LOCATION:x\\, y\\; z", ics)


class Notify(unittest.TestCase):
    """Runs notify.main() offline (no token, no ntfy topic): state handling only."""

    def run_with(self, report):
        with tempfile.TemporaryDirectory() as d:
            notify.REPORT, notify.HEALTH = Path(d) / "r.json", Path(d) / "h.json"
            notify.TOKEN, notify.NTFY_TOPIC = "", ""
            if report is not None:
                notify.REPORT.write_text(json.dumps(report))
            self.assertEqual(notify.main(), 0)
            return json.loads(notify.HEALTH.read_text())

    def test_missing_report_is_an_error(self):
        h = self.run_with(None)
        self.assertTrue(h["digest"])
        self.assertIn("crashed", h["problems"][0])

    def test_watch_filter(self):
        old = notify.WATCH
        notify.WATCH = ["m-26-04-eng-october"]
        try:
            h = self.run_with({
                "errors": [], "changes": [],
                "groups": {"M 26-04 eng october": "m-26-04-eng-october", "M 25-16 eng": "m-25-16-eng"},
                "warnings": ["[t] M 25-16 eng: no room found in 'x'",
                             "[t] M 26-04 eng october: no room found in 'y'",
                             "[t] Layout: time column 8 labels 70 slots, first block labels 72"]})
        finally:
            notify.WATCH = old
        self.assertEqual(len(h["problems"]), 2, h["problems"])
        self.assertFalse(any("M 25-16" in p for p in h["problems"]))

    def test_clean_report(self):
        h = self.run_with({"errors": [], "warnings": [], "changes": []})
        self.assertEqual(h, {"digest": "", "problems": []})


if __name__ == "__main__":
    unittest.main()
