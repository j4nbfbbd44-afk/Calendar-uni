#!/usr/bin/env python3
"""Send notifications for the last fetch (reads _report/report.json).

Channels (stdlib only):
- GitHub Issues (needs GITHUB_TOKEN + GITHUB_REPOSITORY, both present in Actions):
    * one open issue labelled `timetable-health` for problems; a comment is added
      only when the set of problems changes; closed automatically when fixed
    * one open issue labelled `timetable-changes`; one comment per run with changes
  GitHub then notifies you by email / the GitHub mobile app.
- ntfy.sh push (optional): set the NTFY_TOPIC secret to a hard-to-guess topic name and
  subscribe to it in the ntfy app.

WATCH_GROUPS (comma separated slugs or names): only notify about changes and
group-specific warnings for these groups. Warnings that affect every group (fetch or
parse failures, layout drift, stale week) are always sent. Empty = all groups.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "_report" / "report.json"
HEALTH = ROOT / "data" / "health.json"

REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
RUN_URL = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{REPO}/actions/runs/"
           f"{os.environ.get('GITHUB_RUN_ID', '')}") if REPO else ""
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
SITE_URL = os.environ.get("SITE_URL", "").strip()
# GitHub user to @mention so alerts reach email / the GitHub app (mentions notify by default).
MENTION = os.environ.get("NOTIFY_MENTION", "").strip().lstrip("@")
WATCH = [w.strip().lower() for w in os.environ.get("WATCH_GROUPS", "").split(",") if w.strip()]

failures: list[str] = []


# --------------------------------------------------------------------------
def gh(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        f"{API}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "timetable-feed"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        return json.loads(data) if data else None


def ensure_label(name: str, color: str, desc: str):
    try:
        gh("POST", f"/repos/{REPO}/labels", {"name": name, "color": color, "description": desc})
    except urllib.error.HTTPError as e:
        if e.code != 422:  # 422 = already exists
            raise


def open_issue(label: str):
    issues = gh("GET", f"/repos/{REPO}/issues?state=open&labels={urllib.parse.quote(label)}&per_page=1")
    return issues[0] if issues else None


def post_issue(label: str, title: str, body: str, color: str, desc: str):
    """Comment on the open issue with this label, or open a new one."""
    body = body[:60000]
    issue = open_issue(label)
    if issue:
        gh("POST", f"/repos/{REPO}/issues/{issue['number']}/comments", {"body": body})
    else:
        ensure_label(label, color, desc)
        gh("POST", f"/repos/{REPO}/issues", {"title": title, "body": body, "labels": [label]})


def close_issue(label: str, comment: str):
    issue = open_issue(label)
    if issue:
        gh("POST", f"/repos/{REPO}/issues/{issue['number']}/comments", {"body": comment})
        gh("PATCH", f"/repos/{REPO}/issues/{issue['number']}", {"state": "closed", "state_reason": "completed"})


def gh_test_issue(body: str):
    ensure_label("timetable-test", "c5def5", "Test notifications")
    issue = gh("POST", f"/repos/{REPO}/issues",
               {"title": "Timetable: test notification", "body": body, "labels": ["timetable-test"]})
    gh("PATCH", f"/repos/{REPO}/issues/{issue['number']}", {"state": "closed", "state_reason": "completed"})


def ntfy(title: str, message: str, priority: str = "default", tags: str = ""):
    if not NTFY_TOPIC:
        return
    q = {"title": title, "priority": priority}
    if tags:
        q["tags"] = tags
    if SITE_URL:
        q["click"] = SITE_URL
    url = f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC)}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, data=message[:3500].encode("utf-8"), method="POST",
                                 headers={"User-Agent": "timetable-feed"})
    urllib.request.urlopen(req, timeout=30).read()


def attempt(what: str, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except Exception as e:  # keep going with the other channels
        failures.append(f"{what}: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------
def main() -> int:
    if REPORT.exists():
        report = json.loads(REPORT.read_text(encoding="utf-8"))
    else:
        report = {"errors": ["The scraper crashed before writing its report; see the workflow log."],
                  "warnings": [], "changes": []}
    github = bool(TOKEN and REPO)
    footer = (f"\n\n[Workflow run]({RUN_URL})" + (f" · [Website]({SITE_URL})" if SITE_URL else "")
              + (f"\n\ncc @{MENTION}" if MENTION else ""))

    if "--test" in sys.argv:
        msg = "Test notification from the timetable scraper. If you can read this, alerts reach you here."
        if github:
            attempt("GitHub test issue", gh_test_issue, msg + footer)
        attempt("ntfy test", ntfy, "Timetable: test notification", msg, "default", "bell")
        if not NTFY_TOPIC:
            print("ntfy: skipped (no NTFY_TOPIC secret set)")
        for f in failures:
            print(f"NOTIFY FAILED: {f}", file=sys.stderr)
        return 1 if failures else 0

    # ---- health --------------------------------------------------------
    names = report.get("groups", {})  # group name -> slug

    def relevant(w: str) -> bool:
        """Drop warnings about a specific group that is not watched."""
        if not WATCH:
            return True
        body = w.split("] ", 1)[1] if w.startswith("[") and "] " in w else w
        for name, slug in names.items():
            if body.startswith(name + ":"):
                return name.lower() in WATCH or slug.lower() in WATCH
        return True

    warnings = [w for w in report["warnings"] if relevant(w)]
    problems = [f"ERROR: {e}" for e in report["errors"]] + [f"Warning: {w}" for w in warnings]
    digest = hashlib.sha1("\n".join(sorted(problems)).encode()).hexdigest() if problems else ""
    prev = json.loads(HEALTH.read_text(encoding="utf-8")) if HEALTH.exists() else {"digest": ""}
    if problems and digest != prev.get("digest"):
        body = ("The timetable scraper found problems. Feeds and the website keep serving the last good "
                "data for anything that could not be parsed.\n\n" + "\n".join(f"- {p}" for p in problems) + footer)
        if github:
            attempt("GitHub health issue", post_issue, "timetable-health", "Timetable scraper needs attention",
                    body, "d73a4a", "Problems found while reading the timetable sheet")
        attempt("ntfy health", ntfy, "Timetable: problem detected" if report["errors"] else "Timetable: warning",
                "\n".join(problems), "high" if report["errors"] else "default", "warning")
    elif not problems and prev.get("digest"):
        if github:
            attempt("GitHub health close", close_issue, "timetable-health",
                    "All checks pass again; closing." + footer)
        attempt("ntfy resolved", ntfy, "Timetable: back to normal", "All checks pass again.", "low", "white_check_mark")
    HEALTH.parent.mkdir(parents=True, exist_ok=True)
    HEALTH.write_text(json.dumps({"digest": digest, "problems": problems}, ensure_ascii=False, indent=1),
                      encoding="utf-8")

    # ---- changes -------------------------------------------------------
    changes = report.get("changes", [])
    if WATCH:
        changes = [c for c in changes if c["group"].lower() in WATCH or c["name"].lower() in WATCH]
    if changes:
        by_group: dict[str, list] = {}
        for c in changes:
            by_group.setdefault(f"{c['name']} (week of {c['week']}, {c['why']})", []).append(c)
        sym = {"added": "+", "removed": "−", "changed": "~", "group_added": "+", "group_removed": "−"}
        lines = []
        for head, cs in by_group.items():
            lines.append(f"**{head}**")
            lines += [f"- `{sym.get(c['kind'], '·')}` {c['text']}" for c in cs]
            lines.append("")
        body = "\n".join(lines) + footer
        if github:
            attempt("GitHub changes issue", post_issue, "timetable-changes", "Timetable changes",
                    "Each comment lists timetable changes detected by the scraper.\n\n" + body,
                    "0e8a16", "Changes detected in the published timetable")
        groups = sorted({c["name"] for c in changes})
        title = (f"Timetable changed: {groups[0]}" if len(groups) == 1
                 else f"Timetable changed for {len(groups)} groups")
        plain = "\n".join(f"{c['name']}: {sym.get(c['kind'], '·')} {c['text']}" for c in changes)
        attempt("ntfy changes", ntfy, title, plain, "high" if WATCH else "default", "calendar")

    print(f"problems: {len(problems)} (notified: {bool(problems and digest != prev.get('digest'))}), "
          f"changes notified: {len(changes)}")
    for f in failures:
        print(f"NOTIFY FAILED: {f}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
