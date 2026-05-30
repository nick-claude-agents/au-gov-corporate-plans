"""
BD Brief manager for the Corporate Plans dashboard
==================================================

Generates and stores a per-agency business-development brief for every corporate
plan in the dashboard, and keeps them up to date.

For each agency it writes:
  briefs/<slug>.html   - a standalone brief page (linked from the dashboard)
  briefs/<slug>.json   - the structured analysis
  briefs/briefs-index.json - an index the dashboard fetches to show "BD Brief"
                             buttons (slug -> name, portfolio, plan_url, date,
                             plan_period, confidence)

It reuses analyse_corporate_plan.py for the actual analysis (download + Claude).

Usage:
  # Backfill / refresh every agency in the dashboard (index.html):
  python manage_briefs.py --all
  python manage_briefs.py --all --skip-existing          # resume a run
  python manage_briefs.py --all --limit 10               # pilot
  python manage_briefs.py --all --only "Veterans"        # name substring filter

  # Refresh one agency (used by Check-CorporatePlans.ps1 on a plan update).
  # --email-snippet prints the email-embeddable snippet to stdout.
  python manage_briefs.py --agency "X" --portfolio "Y" --url "..." --email-snippet
"""

import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

import analyse_corporate_plan as core   # reuse the analyser as a library

SCRIPT_DIR = Path(__file__).parent
BRIEFS_DIR = SCRIPT_DIR / "briefs"
INDEX_FILE = BRIEFS_DIR / "briefs-index.json"
DASHBOARD = SCRIPT_DIR / "index.html"
LOG = core.LOG


# ── slug (must match the slugify() in index.html) ──────────────────────────────
def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ── Parse the AGENCIES array out of index.html ─────────────────────────────────
_FIELD = r'%s:\s*"((?:[^"\\]|\\.)*)"'


def _field(key: str, line: str):
    m = re.search(_FIELD % key, line)
    return m.group(1) if m else None


def parse_agencies() -> list[dict]:
    if not DASHBOARD.exists():
        LOG.error("Dashboard not found: %s", DASHBOARD)
        sys.exit(2)
    agencies = []
    for line in DASHBOARD.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith('{ name:'):
            continue
        name = _field("name", line)
        if not name:
            continue
        agencies.append({
            "name": name,
            "portfolio": _field("portfolio", line) or "",
            "url": _field("url", line) or "",
            "urlType": _field("urlType", line) or "",
            "description": _field("description", line) or "",
        })
    return agencies


# Auto-added minor entities carry this boilerplate description; the curated set
# is everything with a real, human-written description.
_BOILERPLATE = "corporate plan added automatically"


def is_curated(agency: dict) -> bool:
    return _BOILERPLATE not in agency.get("description", "")


# ── Brief index (load/save) ────────────────────────────────────────────────────
def load_index() -> dict:
    if INDEX_FILE.exists():
        try:
            return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            LOG.warning("briefs-index.json is corrupt; starting fresh.")
    return {"generated": "", "briefs": {}}


def save_index(index: dict) -> None:
    index["generated"] = date.today().isoformat()
    BRIEFS_DIR.mkdir(exist_ok=True)
    INDEX_FILE.write_text(json.dumps(index, indent=2, ensure_ascii=False),
                          encoding="utf-8")


# ── Standalone brief page ──────────────────────────────────────────────────────
def render_standalone_page(analysis: dict, source_url: str) -> str:
    import html as html_mod
    snippet = core.render_html(analysis)
    name = html_mod.escape(analysis.get("agency_name", "Corporate Plan"))
    generated = date.today().strftime("%d %B %Y")
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BD Brief — {name}</title>
<style>
  body {{ font-family: Arial, sans-serif; color:#222; background:#f0f5f4;
         margin:0; padding:0; }}
  .wrap {{ max-width: 900px; margin: 0 auto; padding: 16px; }}
  header {{ background:#0D3D20; color:#fff; padding:16px 20px; border-radius:8px 8px 0 0; }}
  header a {{ color:#a8cec7; font-size:13px; text-decoration:none; }}
  header h1 {{ font-size:19px; margin:6px 0 0; }}
  .meta {{ font-size:12px; color:#a8cec7; margin-top:4px; }}
  .body {{ background:#fff; padding:8px 20px 20px; border:1px solid #ddd;
          border-top:none; border-radius:0 0 8px 8px; }}
  .disclaimer {{ font-size:12px; color:#777; margin:10px 0 0; }}
</style></head>
<body><div class="wrap">
<header>
  <a href="../index.html">&larr; Back to dashboard</a>
  <h1>{name} — Business Development Brief</h1>
  <div class="meta">Generated {generated} &middot;
    <a href="{html_mod.escape(source_url)}" target="_blank" rel="noopener"
       style="color:#a8cec7">Source corporate plan</a></div>
</header>
<div class="body">
{snippet}
<p class="disclaimer">This brief was drafted by Claude from the agency's
published corporate plan. It is a starting point for business development, not
verified advice — confirm details before acting.</p>
</div>
</div></body></html>"""


# ── Generate / refresh one brief ───────────────────────────────────────────────
def generate_one(agency: str, portfolio: str, url: str, index: dict,
                 research: bool = True) -> dict | None:
    """Analyse a plan, write briefs/<slug>.{html,json}, update the index.
    Returns the analysis dict, or None on failure."""
    slug = slugify(agency)
    try:
        plan_text, kind = core.fetch_plan_text(url)
        analysis = core.analyse_with_claude(agency, portfolio, url, plan_text,
                                            kind, research=research)
    except SystemExit:
        raise                       # missing key / deps — let it propagate
    except Exception as e:          # noqa: BLE001 - report, skip this agency
        LOG.error("Failed to analyse %s: %s", agency, e)
        return None

    BRIEFS_DIR.mkdir(exist_ok=True)
    (BRIEFS_DIR / f"{slug}.json").write_text(
        json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    (BRIEFS_DIR / f"{slug}.html").write_text(
        render_standalone_page(analysis, url), encoding="utf-8")

    index.setdefault("briefs", {})[slug] = {
        "name": agency,
        "portfolio": portfolio,
        "plan_url": url,
        "generated_at": date.today().isoformat(),
        "plan_period": analysis.get("plan_period", ""),
        "overall_confidence": analysis.get("overall_confidence", ""),
    }
    return analysis


# ── Modes ──────────────────────────────────────────────────────────────────────
def run_all(skip_existing: bool, limit: int | None, only: str | None,
            curated: bool = False, research: bool = True) -> int:
    agencies = parse_agencies()
    if curated:
        agencies = [a for a in agencies if is_curated(a)]
    if only:
        agencies = [a for a in agencies if only.lower() in a["name"].lower()]
    index = load_index()
    existing = set(index.get("briefs", {}))

    queue = []
    for a in agencies:
        if not a["url"]:
            continue
        if skip_existing and slugify(a["name"]) in existing:
            continue
        queue.append(a)
    if limit:
        queue = queue[:limit]

    LOG.info("Backfill: %d agencies to process (of %d in dashboard, %d already "
             "have briefs).", len(queue), len(agencies), len(existing))

    ok = fail = 0
    for i, a in enumerate(queue, 1):
        LOG.info("[%d/%d] %s", i, len(queue), a["name"])
        result = generate_one(a["name"], a["portfolio"], a["url"], index, research)
        if result is not None:
            ok += 1
            save_index(index)       # persist after each success (resumable)
        else:
            fail += 1
        time.sleep(1)               # be gentle on source sites
    LOG.info("Backfill complete. Succeeded: %d, failed: %d.", ok, fail)
    # Per-plan failures (e.g. a slow/stale source URL) are expected in a batch and
    # must NOT fail the whole (scheduled) run — successes are still saved/committed.
    return 0


def run_one(agency: str, portfolio: str, url: str, email_snippet: bool,
            research: bool = True) -> int:
    index = load_index()
    analysis = generate_one(agency, portfolio, url, index, research)
    if analysis is None:
        return 1
    save_index(index)
    if email_snippet:
        sys.stdout.write(core.render_html(analysis))   # for the daily email
    else:
        LOG.info("Wrote brief for %s (briefs/%s.html)", agency, slugify(agency))
    return 0


def run_refresh_stale(max_age_days: int, limit: int | None,
                      research: bool = True) -> int:
    """Regenerate every brief older than max_age_days. Regenerating resets the
    brief's generated_at, so each brief refreshes on a rolling cycle anchored to
    when it was first run. Run this daily (Task Scheduler) for a ~2-month cadence.
    Serialised + retry-backed, so it is safe on a low rate-limit tier."""
    index = load_index()
    today = date.today()
    due = []
    for slug, meta in index.get("briefs", {}).items():
        url = meta.get("plan_url", "")
        if not url.startswith("http"):
            LOG.info("Skipping %s — no live plan URL on record (%r).",
                     meta.get("name", slug), url)
            continue
        gen = meta.get("generated_at", "")
        try:
            age = (today - date.fromisoformat(gen)).days
        except ValueError:
            age = max_age_days          # unknown date -> treat as due
        if age >= max_age_days:
            due.append((age, meta))
    due.sort(reverse=True)              # oldest first
    if limit:
        due = due[:limit]

    LOG.info("Refresh: %d brief(s) are >= %d days old and due for update.",
             len(due), max_age_days)
    ok = fail = 0
    for age, meta in due:
        LOG.info("Refreshing %s (%d days old)...", meta["name"], age)
        result = generate_one(meta["name"], meta.get("portfolio", ""),
                              meta["plan_url"], index, research)
        if result is not None:
            ok += 1
            save_index(index)
        else:
            fail += 1
        time.sleep(1)
    LOG.info("Refresh complete. Updated: %d, failed: %d.", ok, fail)
    return 0          # tolerate per-plan failures in the scheduled run


def main() -> int:
    p = argparse.ArgumentParser(description="Manage BD briefs for the dashboard.")
    p.add_argument("--all", action="store_true",
                   help="Backfill/refresh every agency in the dashboard")
    p.add_argument("--skip-existing", action="store_true",
                   help="With --all: skip agencies that already have a brief")
    p.add_argument("--limit", type=int, help="With --all: cap how many to process")
    p.add_argument("--only", help="With --all: only agencies whose name contains this")
    p.add_argument("--curated", action="store_true",
                   help="With --all: skip auto-added minor entities (boilerplate "
                        "description) — analyse only the curated agencies")
    p.add_argument("--agency", help="Single mode: agency name")
    p.add_argument("--portfolio", help="Single mode: portfolio")
    p.add_argument("--url", help="Single mode: plan URL")
    p.add_argument("--email-snippet", action="store_true",
                   help="Single mode: print the email snippet to stdout")
    p.add_argument("--no-research", action="store_true",
                   help="Skip the web-search research step (plan only)")
    p.add_argument("--refresh-stale", action="store_true",
                   help="Regenerate briefs older than --max-age-days (for the "
                        "scheduled 2-monthly refresh)")
    p.add_argument("--max-age-days", type=int, default=60,
                   help="With --refresh-stale: age threshold in days (default 60)")
    args = p.parse_args()

    core.load_env_file()
    research = not args.no_research

    if args.refresh_stale:
        return run_refresh_stale(args.max_age_days, args.limit, research)
    if args.all:
        return run_all(args.skip_existing, args.limit, args.only, args.curated,
                       research)
    if args.agency and args.portfolio and args.url:
        return run_one(args.agency, args.portfolio, args.url, args.email_snippet,
                       research)
    p.error("Provide --all, --refresh-stale, or --agency/--portfolio/--url.")


if __name__ == "__main__":
    sys.exit(main())
