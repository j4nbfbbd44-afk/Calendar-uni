# Timetable feeds

Unofficial, readable version of the university's published Google Sheets timetable:

- a web page with a **Today** view (current/next class + room) and a **week** view per group
- one **iCalendar subscription feed per group** (`ics/<group>.ics`) for Google Calendar, Proton Calendar, Apple Calendar, Thunderbird, KDE, etc.

## How it works

`scraper/timetable.py` (Python stdlib only):

1. `fetch` downloads the sheet's "publish to web" HTML (`/pubhtml`), finds every published tab whose name
   contains a week range like `23.11-29.11`, parses it **with merged cells expanded** (shared classes
   span several group columns), and writes `data/weeks/<monday>.json`. Weeks are kept in git, so past
   weeks stay in the feeds after the university replaces the tab.
2. `build` turns `data/weeks/` + `site/` into `_site/` (web page, per-group JSON, per-group `.ics`).

`.github/workflows/update.yml` runs this every 2 hours and deploys `_site/` to GitHub Pages.
If a parse looks wrong (too few groups, no events, no date) the last good data is kept.

Only the schedule grid is extracted. Rows below it (student lists, advisor names, phone numbers,
WhatsApp links) and the teacher/room occupancy matrices are ignored on purpose.

## Parsing assumptions (will break if the sheet layout changes)

- Group names are in the row directly above the first `8.00-9.00` row; program titles one row above that.
- Each program block has a day column (`пн / mon` …) immediately left of a time column (`8.00-9.00` …).
- The group area ends at the `TEACHERS` column.
- A time written inside a cell (e.g. `физ.ра спорт комплекс 16:20-18:20`) overrides the grid slot times.
- Consecutive identical hourly cells are merged into one event.
- Year comes from the document title (`2026-2027`: Aug–Dec → 2026, Jan–Jul → 2027).
- Times are Asia/Almaty (UTC+5).
- Room/location is extracted best effort; the event title always keeps the original cell text.

## Run locally

```sh
python3 scraper/timetable.py all
cd _site && python3 -m http.server
```

## Setup

Settings → Pages → Source: **GitHub Actions**. Scheduled workflows only run on the default branch.
