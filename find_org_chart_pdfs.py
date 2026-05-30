#!/usr/bin/env python3
"""Find & download each agency's published org-chart PDF from its website.

Primary source for org charts = the PDF most departments/agencies publish
(usually under About us / Our organisation). Where none is found, the
directory.gov.au structured chart (scrape_org_charts.py) remains the fallback.

Strategy per agency (uses the website URL already captured in
org_charts/<slug>.json):
  1. Fetch the homepage; collect every link.
  2. Direct hits: links whose text/href look like an org chart AND end .pdf.
  3. Otherwise follow a few likely section pages (About / Our organisation /
     Structure / Executive / Leadership / Corporate) one level deep and look
     for the PDF there.
  4. Score candidates, download the best, verify it is a real PDF.

Outputs:
  org_charts/pdf/<slug>.pdf      downloaded charts
  org_charts/pdf_index.json      {slug,title,website,pdf_url,source,status}

Usage:
  python find_org_chart_pdfs.py [--only "defence"] [--limit N]
                                [--refresh] [--workers 6]
"""
import argparse
import concurrent.futures as cf
import glob
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
CH_DIR = HERE / "org_charts"
PDF_DIR = CH_DIR / "pdf"
PDF_INDEX = CH_DIR / "pdf_index.json"
OVERRIDES = CH_DIR / "pdf_overrides.json"   # {slug: direct_pdf_url} from search
UA = "Mozilla/5.0 (org-chart-finder; Parbery BD tooling)"
TIMEOUT = 20

# Phrases that signal an org chart, strongest first.
CHART_PAT = re.compile(
    r"organisation(?:al)?[\s\-_]*(?:chart|structure)|"
    r"organization(?:al)?[\s\-_]*(?:chart|structure)|"
    r"\borg[\s\-_]*(?:chart|structure)|structure[\s\-_]*chart|"
    r"executive[\s\-_]*structure|leadership[\s\-_]*structure|"
    r"(?:our|agency|departmental|corporate)[\s\-_]*structure",
    re.I)
# A PDF is only accepted as an org chart if its filename/link clearly says so.
STRONG_PAT = re.compile(
    r"org[\s\-_]?chart|orgchart|"
    r"organisation(?:al)?[\s\-_]?(?:chart|structure)|"
    r"organization(?:al)?[\s\-_]?(?:chart|structure)|"
    r"\borg[\s\-_]?structure|structure[\s\-_]?chart|"
    r"(?:corporate|agency|departmental)[\s\-_]?structure", re.I)
# Section pages worth following one level deep to find the chart.
SECTION_PAT = re.compile(
    r"about[\s\-_]*us|about\b|our[\s\-_]*organisation|who[\s\-_]*we[\s\-_]*are|"
    r"\bstructure\b|executive|senior[\s\-_]*(?:leaders|executive|management)|"
    r"leadership|our[\s\-_]*people|corporate", re.I)


def load_agencies():
    out = []
    for f in glob.glob(str(CH_DIR / "*.json")):
        if f.endswith("index.json") or f.endswith("pdf_index.json"):
            continue
        d = json.load(open(f, encoding="utf-8"))
        if d.get("website"):
            out.append({"slug": Path(f).stem, "title": d["title"],
                        "website": d["website"].strip()})
    out.sort(key=lambda a: a["title"].lower())
    return out


def score(text, href):
    """Higher = more likely the real org-chart PDF."""
    blob = f"{text} {href}".lower()
    s = 0
    if CHART_PAT.search(blob):
        s += 100
    if "organisation" in blob and "chart" in blob:
        s += 40
    if href.lower().endswith(".pdf"):
        s += 20
    # Penalise obvious distractors.
    if re.search(r"annual[\s\-_]*report|corporate[\s\-_]*plan|budget", blob):
        s -= 60
    return s


def get(session, url):
    try:
        r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 200:
            return r
    except requests.RequestException:
        pass
    return None


def links(html, base):
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        text = " ".join(a.get_text(" ", strip=True).split())
        yield text, urljoin(base, href)


# Likely paths to probe directly when the homepage doesn't link the chart.
COMMON_PATHS = [
    "/about/organisational-structure", "/about-us/organisational-structure",
    "/about/our-organisation", "/about-us/our-organisation",
    "/organisational-structure", "/organisation-structure",
    "/about/structure", "/about-us/structure", "/our-structure",
    "/about/executive", "/about-us/executive", "/about/leadership",
    "/about/who-we-are", "/about-us/who-we-are", "/about/governance",
]
MIN_SCORE = 60  # a PDF must clearly read as a chart unless found on a chart page


def harvest(session, page, candidates, chart_page=False):
    """Pull PDF candidates from a fetched page into `candidates`."""
    for text, u in links(page.text, page.url):
        if not u.lower().split("?")[0].endswith(".pdf"):
            continue
        # Require an explicit org-chart token in the link/filename — being on a
        # structure page is not enough (avoids grabbing charters, RAPs, minutes).
        if not STRONG_PAT.search(f"{text} {u}"):
            continue
        candidates.append((score(text, u) + (15 if chart_page else 0), text, u))


def download_pdf(session, slug, url):
    """Download a PDF if `url` really serves one. Returns size or None."""
    pdf = get(session, url)
    if not pdf or not pdf.content[:5].startswith(b"%PDF"):
        return None
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    (PDF_DIR / f"{slug}.pdf").write_bytes(pdf.content)
    return len(pdf.content)


def find_for(agency):
    """Return a result dict for one agency."""
    res = {"slug": agency["slug"], "title": agency["title"],
           "website": agency["website"], "pdf_url": None,
           "source": None, "status": "not_found"}
    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    # Authoritative search-found URL wins over crawling.
    override = agency.get("override")
    if override:
        size = download_pdf(session, agency["slug"], override)
        if size:
            res.update(pdf_url=override, source="search",
                       status="downloaded", size=size)
            return res
        res.update(pdf_url=override, status="pdf_download_failed",
                   source="search")
        return res

    home = get(session, agency["website"])
    if not home:
        res["status"] = "site_unreachable"
        return res

    base = f"{urlparse(home.url).scheme}://{urlparse(home.url).netloc}"
    candidates = []                 # (score, text, url)
    chart_pages, section_pages = [], []
    for text, url in links(home.text, home.url):
        blob = f"{text} {url}"
        if url.lower().split("?")[0].endswith(".pdf"):
            if STRONG_PAT.search(blob):
                candidates.append((score(text, url), text, url))
        elif CHART_PAT.search(blob):
            chart_pages.append(url)          # e.g. "Organisational structure" page
        elif SECTION_PAT.search(blob):
            section_pages.append((score(text, url), url))

    # Follow dedicated chart pages first, then likely section pages, then a few
    # guessed common paths — stop early once we have a strong candidate.
    seen = set()
    section_pages.sort(key=lambda x: -x[0])
    to_visit = ([(u, True) for u in chart_pages] +
                [(u, False) for _, u in section_pages[:5]] +
                [(base + p, True) for p in COMMON_PATHS])
    for url, is_chart in to_visit:
        if any(c[0] >= 100 for c in candidates):
            break
        if url in seen:
            continue
        seen.add(url)
        page = get(session, url)
        if page and "pdf" not in page.headers.get("content-type", ""):
            harvest(session, page, candidates, chart_page=is_chart)
        time.sleep(0.2)

    if not candidates:
        return res
    candidates.sort(key=lambda x: -x[0])
    best_score, _, best_url = candidates[0]

    size = download_pdf(session, agency["slug"], best_url)
    if not size:
        res.update(status="pdf_download_failed", pdf_url=best_url)
        return res
    res.update(pdf_url=best_url, source="website",
               status="downloaded", score=best_score, size=size)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="substring filter on agency title/slug")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--refresh", action="store_true",
                    help="re-attempt agencies already downloaded")
    args = ap.parse_args()

    agencies = load_agencies()
    overrides = json.load(open(OVERRIDES, encoding="utf-8")) if OVERRIDES.exists() else {}
    for a in agencies:
        if a["slug"] in overrides:
            a["override"] = overrides[a["slug"]]
    if args.only:
        q = args.only.lower()
        agencies = [a for a in agencies if q in a["title"].lower() or q in a["slug"]]
    if not args.refresh and PDF_INDEX.exists():
        done = {r["slug"] for r in json.load(open(PDF_INDEX, encoding="utf-8"))
                ["results"] if r["status"] == "downloaded"}
        agencies = [a for a in agencies if a["slug"] not in done]
    if args.limit:
        agencies = agencies[:args.limit]

    print(f"Scanning {len(agencies)} agency sites with {args.workers} workers…")
    results = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for r in ex.map(find_for, agencies):
            tick = {"downloaded": "OK ", "not_found": "-- ",
                    "site_unreachable": "XX ",
                    "pdf_download_failed": "?? "}.get(r["status"], "?? ")
            print(f"  {tick}{r['title'][:48]:48} {r.get('pdf_url') or r['status']}")
            results.append(r)

    # Merge with any previous results so the index stays complete.
    if PDF_INDEX.exists():
        prev = {x["slug"]: x for x in
                json.load(open(PDF_INDEX, encoding="utf-8"))["results"]}
    else:
        prev = {}
    for r in results:
        prev[r["slug"]] = r
    merged = sorted(prev.values(), key=lambda x: x["title"].lower())
    got = sum(1 for r in merged if r["status"] == "downloaded")
    PDF_INDEX.write_text(json.dumps(
        {"downloaded": got, "total": len(merged), "results": merged},
        indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nPDFs downloaded: {got}/{len(merged)} (index -> {PDF_INDEX.name})")


if __name__ == "__main__":
    main()
