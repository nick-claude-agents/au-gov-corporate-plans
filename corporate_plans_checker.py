"""
Australian Government Corporate Plans Checker
Runs daily via GitHub Actions to:
1. Check all agencies in the dashboard for updated plan URLs
2. Discover new agencies via the Transparency Portal API
3. Update index.html with any new agencies
4. Email a summary to nick.chapman@parbery.com.au
"""

import json
import os
import re
import smtplib
import ssl
import time
import logging
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ── Configuration ─────────────────────────────────────────────────────────────
CONFIG = {
    "from_email":    "nick.claude.agents@gmail.com",
    "to_email":      "nick.chapman@parbery.com.au",
    "html_file":     Path("index.html"),
    "state_file":    Path("known-urls.json"),
    "log_file":      Path("checker.log"),
    "project_id":    "80a82ed1-3e33-027b-b7e0-6493f97f18f8",
    "dashboard_url": "https://nick-claude-agents.github.io/au-gov-corporate-plans/",
}

GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CorporatePlanChecker/2.0)"}

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(CONFIG["log_file"], encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── Load agencies from index.html ─────────────────────────────────────────────
def load_agencies():
    html = CONFIG["html_file"].read_text(encoding="utf-8")
    pattern = r'\{ name: "([^"]+)", portfolio: "([^"]+)", type: "([^"]+)", url: "([^"]+)", urlType: "([^"]+)", description: "([^"]+)" \}'
    return [
        {"name": m.group(1), "portfolio": m.group(2), "type": m.group(3),
         "url": m.group(4), "url_type": m.group(5), "description": m.group(6)}
        for m in re.finditer(pattern, html)
    ]

# ── URL utilities ─────────────────────────────────────────────────────────────
YEAR_SUBS = [
    ("2024-25",   "2025-26"),
    ("2024-2025", "2025-2026"),
    ("202425",    "202526"),
    ("2024_25",   "2025_26"),
    ("fy-2024",   "fy-2025"),
    ("fy2024",    "fy2025"),
    ("24-25",     "25-26"),
    ("/2024/",    "/2025/"),
]

def candidate_urls(url):
    seen = {}
    for old, new in YEAR_SUBS:
        if old in url:
            c = url.replace(old, new)
            seen[c] = True
    return list(seen.keys())

def url_live(url, timeout=12):
    try:
        r = requests.head(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code < 400:
            return True
    except Exception:
        pass
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, stream=True)
        r.close()
        return r.status_code < 400
    except Exception:
        return False

# ── Transparency Portal API ───────────────────────────────────────────────────
def get_portal_entities():
    pid = CONFIG["project_id"]
    aest = timezone(timedelta(hours=10))
    now = datetime.now(aest)
    fy_start = now.year if now.month >= 7 else now.year - 1
    fy_code = f"n{fy_start}_{str(fy_start + 1)[2:]}"

    api = f"https://deliver.kontent.ai/{pid}/items"
    results, skip, has_more = [], 0, True
    log.info(f"Querying Transparency Portal (FY codename: {fy_code})...")

    while has_more:
        try:
            r = requests.get(api, headers=HEADERS, timeout=20, params={
                "system.type": "corp_plan",
                "elements.reporting_period[contains]": fy_code,
                "limit": 100, "skip": skip, "depth": 0,
            })
            data = r.json()
        except Exception as e:
            log.warning(f"API error (skip={skip}): {e}")
            break

        for item in data.get("items", []):
            raw = item["system"]["name"]
            name = re.sub(r'^\d{4}[-–/]\d{2,4}\s+', '', raw)
            name = re.sub(r'\s+Corporate Plan.*$', '', name, flags=re.IGNORECASE).strip()
            if len(name) < 3:
                name = raw

            web  = item["elements"].get("corp_plan_url", {}).get("value", "")
            pdfs = item["elements"].get("pdf_file", {}).get("value", [])
            if web:
                plan_url, url_type = web, "web"
            elif pdfs:
                cdn = pdfs[0]["url"]
                m = re.search(r'[a-f0-9-]{36}/([a-f0-9-]{36})/(.+?)(?:\?|$)', cdn)
                if m:
                    plan_url = f"https://previewapi.transparency.gov.au/delivery/assets/{pid}/{m.group(1)}/{m.group(2)}"
                else:
                    plan_url = cdn
                url_type = "pdf"
            else:
                slug = item["elements"].get("url_slug", {}).get("value", "")
                plan_url = f"https://www.transparency.gov.au/publications/corporate-plans/{slug}"
                url_type = "web"

            results.append({"name": name, "url": plan_url, "url_type": url_type, "raw": raw})

        has_more = bool(data.get("pagination", {}).get("next_page", ""))
        skip += 100

    log.info(f"Portal returned {len(results)} plans")
    return results

# ── HTML update ───────────────────────────────────────────────────────────────
def normalize(name):
    name = re.sub(r'\([^)]*\)', '', name.lower())
    name = re.sub(r'department of the ', 'department of ', name)
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9 ]', '', name)).strip()

def add_agency_to_html(name, portfolio, plan_url, url_type):
    html = CONFIG["html_file"].read_text(encoding="utf-8")
    array_end = html.index('];')
    safe_name = name.replace('"', '\\"')
    safe_port = portfolio.replace('"', '\\"')
    entry = f'  {{ name: "{safe_name}", portfolio: "{safe_port}", type: "Commonwealth Entity", url: "{plan_url}", urlType: "{url_type}", description: "Commonwealth entity - corporate plan added automatically" }},'

    array_block = html[:array_end]
    insert_pos = None
    for m in re.finditer(r'\{ name: "([^"]+)"', array_block):
        if m.group(1).lower() > name.lower():
            insert_pos = m.start()
            break

    if insert_pos is not None:
        html = html[:insert_pos] + entry + "\n" + html[insert_pos:]
    else:
        html = html[:array_end] + entry + "\n" + html[array_end:]

    CONFIG["html_file"].write_text(html, encoding="utf-8")

# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(subject, body):
    if not GMAIL_APP_PASSWORD:
        log.warning("GMAIL_APP_PASSWORD not set - skipping email")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"AU Gov Plan Checker <{CONFIG['from_email']}>"
        msg["To"] = CONFIG["to_email"]
        msg.attach(MIMEText(body, "html"))
        ctx = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls(context=ctx)
            s.login(CONFIG["from_email"], GMAIL_APP_PASSWORD)
            s.sendmail(CONFIG["from_email"], CONFIG["to_email"], msg.as_string())
        log.info(f"Email sent: {subject}")
    except Exception as e:
        log.error(f"Email failed: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    aest = timezone(timedelta(hours=10))
    date_str = datetime.now(aest).strftime("%A %d %B %Y")

    known_urls = {}
    if CONFIG["state_file"].exists():
        known_urls = json.loads(CONFIG["state_file"].read_text(encoding="utf-8"))

    agencies = load_agencies()
    log.info(f"Starting check for {len(agencies)} agencies...")

    updates, broken, unchanged = [], [], 0

    for a in agencies:
        found_new = next((c for c in candidate_urls(a["url"]) if url_live(c)), None)
        current_live = True if found_new else url_live(a["url"])

        if found_new:
            log.info(f"UPDATE: {a['name']} -> {found_new}")
            updates.append({"name": a["name"], "portfolio": a["portfolio"], "old": a["url"], "new": found_new})
            known_urls[a["name"]] = found_new
        elif not current_live:
            log.info(f"BROKEN: {a['name']}")
            broken.append({"name": a["name"], "portfolio": a["portfolio"], "url": a["url"]})
        else:
            unchanged += 1
        time.sleep(0.3)

    log.info(f"URL check done. Updates={len(updates)} Broken={len(broken)} Unchanged={unchanged}")

    # Discover new agencies
    log.info("Scanning Transparency Portal for new agencies...")
    new_agencies = []
    known_norms = {normalize(a["name"]) for a in agencies}

    for entity in get_portal_entities():
        norm = normalize(entity["name"])
        already = any(
            n == norm or n in norm or norm in n or
            (len(n) > 8 and len(norm) > 8 and n.replace(" ", "") == norm.replace(" ", ""))
            for n in known_norms
        )
        if already or entity["name"] in known_urls:
            continue

        log.info(f"NEW: {entity['name']}")
        n = entity["name"].lower()
        if   any(x in n for x in ['health','aged care','therapeutic','nhmrc','blood','aihw','arpansa']): portfolio = "Health"
        elif any(x in n for x in ['defence','military','signal','veteran','war memorial']):              portfolio = "Defence"
        elif any(x in n for x in ['treasury','tax','asic','apra','accc','housing australia']):          portfolio = "Treasury"
        elif any(x in n for x in ['home affairs','border','federal police','asio','austrac','acic']):   portfolio = "Home Affairs"
        elif any(x in n for x in ['finance','audit','electoral','digital transform']):                  portfolio = "Finance"
        elif any(x in n for x in ['education','research council','teqsa']):                             portfolio = "Education"
        elif any(x in n for x in ['industry','science','resources','csiro','geoscience','marine science']): portfolio = "Industry, Science and Resources"
        elif any(x in n for x in ['infrastructure','transport','communications','screen','creative','library','gallery','museum','casa']): portfolio = "Infrastructure, Transport, Regional Development, Communications and the Arts"
        elif any(x in n for x in ['climate','environment','energy','water','meteorology','clean energy']): portfolio = "Climate Change, Energy, the Environment and Water"
        elif any(x in n for x in ['social','ndis','disability','services australia']):                  portfolio = "Social Services"
        elif any(x in n for x in ['employment','workplace','fair work','skills']):                      portfolio = "Employment and Workplace Relations"
        elif any(x in n for x in ['attorney','legal','ombudsman','human rights','afsa','criminology']): portfolio = "Attorney-General"
        elif any(x in n for x in ['prime minister','cabinet','indigenous','niaa','public service','archives','aiatsis']): portfolio = "Prime Minister and Cabinet"
        elif any(x in n for x in ['foreign','trade','austrade','dfat','aciar','export']):               portfolio = "Foreign Affairs and Trade"
        elif any(x in n for x in ['veteran','war memorial']):                                          portfolio = "Veterans' Affairs"
        elif any(x in n for x in ['agriculture','fisheries','forestry']):                              portfolio = "Agriculture, Fisheries and Forestry"
        else:                                                                                           portfolio = "Commonwealth Entity"

        add_agency_to_html(entity["name"], portfolio, entity["url"], entity["url_type"])
        known_urls[entity["name"]] = entity["url"]
        known_norms.add(norm)
        new_agencies.append({"name": entity["name"], "portfolio": portfolio, "url": entity["url"]})
        log.info(f"Added: {entity['name']} [{portfolio}]")

    CONFIG["state_file"].write_text(json.dumps(known_urls, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"Discovery done. New={len(new_agencies)}")

    # Build email
    has_news = updates or broken or new_agencies
    if updates and new_agencies:
        subject = f"AU Gov Corporate Plans - {len(updates)} updated, {len(new_agencies)} new - {date_str}"
    elif updates:
        subject = f"AU Gov Corporate Plans - {len(updates)} plan update(s) - {date_str}"
    elif new_agencies:
        subject = f"AU Gov Corporate Plans - {len(new_agencies)} new agency(s) added - {date_str}"
    elif broken:
        subject = f"AU Gov Corporate Plans - {len(broken)} broken link(s) - {date_str}"
    else:
        subject = f"AU Gov Corporate Plans - No changes - {date_str}"

    html = f"""<html><body style='font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:700px'>
<div style='background:#19473c;color:white;padding:16px 20px;border-radius:8px 8px 0 0'>
  <h2 style='margin:0;font-size:18px'>Australian Government Corporate Plans</h2>
  <p style='margin:4px 0 0;color:#a8cec7;font-size:13px'>Daily Update Report - {date_str}</p>
</div>
<div style='border:1px solid #d0e0dc;border-top:none;padding:20px;border-radius:0 0 8px 8px'>"""

    if updates:
        html += f"<h3 style='color:#19473c;margin-top:0'>New Plans Detected ({len(updates)})</h3>"
        html += "<table style='width:100%;border-collapse:collapse;font-size:13px'><tr style='background:#fff3f4'><th style='padding:8px;text-align:left;border-bottom:2px solid #d0e0dc'>Agency</th><th style='padding:8px;text-align:left;border-bottom:2px solid #d0e0dc'>Portfolio</th><th style='padding:8px;text-align:left;border-bottom:2px solid #d0e0dc'>New Plan</th><th style='padding:8px;text-align:left;border-bottom:2px solid #d0e0dc'>Previous</th></tr>"
        for u in updates:
            html += f"<tr style='border-bottom:1px solid #eee'><td style='padding:8px;font-weight:bold'>{u['name']}</td><td style='padding:8px;color:#666;font-size:12px'>{u['portfolio']}</td><td style='padding:8px'><a href='{u['new']}' style='color:#1f735e'>Open new plan</a></td><td style='padding:8px'><a href='{u['old']}' style='color:#999;font-size:12px'>Old link</a></td></tr>"
        html += "</table><br>"

    if new_agencies:
        html += f"<h3 style='color:#6A1B9A;margin-top:0'>New Agencies Added ({len(new_agencies)})</h3>"
        html += "<table style='width:100%;border-collapse:collapse;font-size:13px'><tr style='background:#F3E5F5'><th style='padding:8px;text-align:left;border-bottom:2px solid #d0e0dc'>Agency</th><th style='padding:8px;text-align:left;border-bottom:2px solid #d0e0dc'>Portfolio</th><th style='padding:8px;text-align:left;border-bottom:2px solid #d0e0dc'>Corporate Plan</th></tr>"
        for n in new_agencies:
            html += f"<tr style='border-bottom:1px solid #eee'><td style='padding:8px;font-weight:bold'>{n['name']}</td><td style='padding:8px;color:#666;font-size:12px'>{n['portfolio']}</td><td style='padding:8px'><a href='{n['url']}' style='color:#6A1B9A'>Open plan</a></td></tr>"
        html += "</table><br>"

    if broken:
        html += f"<h3 style='color:#BF360C'>Broken Links ({len(broken)})</h3><ul style='font-size:13px'>"
        for b in broken:
            html += f"<li><strong>{b['name']}</strong> ({b['portfolio']}) - <a href='{b['url']}' style='color:#BF360C'>{b['url']}</a></li>"
        html += "</ul><br>"

    if not has_news:
        html += f"<p style='color:#1f735e;font-size:15px;font-weight:bold'>No changes detected</p><p style='color:#555;font-size:13px'>All {len(agencies)} agencies checked - no updates, no new agencies, no broken links.</p>"

    html += f"""<hr style='border:none;border-top:1px solid #d0e0dc;margin:20px 0'>
<p style='font-size:12px;color:#999'>Checked: {len(agencies)} | Updated: {len(updates)} | New: {len(new_agencies)} | Broken: {len(broken)} | Unchanged: {unchanged}<br>
<a href='{CONFIG["dashboard_url"]}' style='color:#1f735e'>Open live dashboard</a></p>
</div></body></html>"""

    send_email(subject, html)

if __name__ == "__main__":
    main()
