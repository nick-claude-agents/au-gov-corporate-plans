#!/usr/bin/env python3
"""Build org charts for Australian Government agencies.

Source: directory.gov.au full XML export (the Organisation & Appointments
Register). One <item> per body. We reconstruct, for every `organisation`:

    organisation
      -> directory_sub_structure (divisions/branches, nested)
           -> roles (directory_role / single_executive_role / role)
                -> person (contact)

Linkage fields:
    parent_directory_structure -> content_id of parent (org or sub-structure)
    parent_organisation        -> content_id of the root organisation
    role_belongs_to            -> content_id of the structure a role sits under
    contact                    -> content_id of the person filling a role
    importance                 -> rank within a structure (1 = most senior)

Outputs:
    directory_export.xml        cached source (re-download with --refresh)
    org_charts/<slug>.json      one nested chart per organisation
    org_charts/index.json       catalogue + dashboard-match status
    org_charts.html             standalone collapsible viewer

Usage:
    python scrape_org_charts.py [--refresh] [--only "Department of"] [--quiet]
"""
import argparse
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
XML_URL = "https://www.directory.gov.au/sites/default/files/export.xml"
XML_PATH = HERE / "directory_export.xml"
OUT_DIR = HERE / "org_charts"
INDEX_HTML = HERE / "index.html"
UA = "Mozilla/5.0 (org-chart-scraper; Parbery BD tooling)"

ROLE_TYPES = {"directory_role", "single_executive_role", "role", "portfolio_role"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def download_xml():
    log(f"Downloading {XML_URL} ...")
    req = urllib.request.Request(XML_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    XML_PATH.write_bytes(data)
    log(f"Saved {len(data):,} bytes -> {XML_PATH.name}")


def text(item, field, default=""):
    el = item.find(field)
    if el is None or el.text is None:
        return default
    return html.unescape(el.text.strip())


def slugify(name):
    s = re.sub(r"\([^)]*\)", "", name).lower()          # drop "(ACQSC)" etc.
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "agency"


def norm(name):
    """Normalise a name for matching: lowercase, drop parens & punctuation."""
    s = re.sub(r"\([^)]*\)", " ", name.lower())
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def name_candidates(name):
    """Alternate names to match on: full, the part before '(', and each
    parenthetical alias (e.g. 'Austrade (Australian Trade and Investment
    Commission)' -> {'austrade ...', 'austrade', 'australian trade ...'})."""
    cands = {norm(name)}
    before = name.split("(", 1)[0]
    cands.add(norm(before))
    for inside in re.findall(r"\(([^)]*)\)", name):
        for part in inside.split(","):
            cands.add(norm(part))
    return {c for c in cands if len(c) >= 4}


def load_dashboard_agencies():
    if not INDEX_HTML.exists():
        return []
    html = INDEX_HTML.read_text(encoding="utf-8")
    block = re.search(r"const AGENCIES\s*=\s*\[(.*?)\];", html, re.S)
    scope = block.group(1) if block else html
    names = re.findall(r'name:\s*"((?:[^"\\]|\\.)*)"', scope)
    return [n.replace('\\"', '"') for n in names]


def match_to_dashboard(orgs, agencies):
    """Return {org_content_id: dashboard_name} best-effort matches."""
    # Index register orgs by every name candidate (full / pre-paren / aliases).
    cand_index = {}
    for o in orgs:
        for c in name_candidates(o["title"]):
            cand_index.setdefault(c, o["content_id"])
        for a in (o.get("acronym"), o.get("org_acronym")):
            if a and len(a) >= 2:
                cand_index.setdefault(norm(a), o["content_id"])
    matches = {}
    used = set()
    for name in agencies:
        cands = name_candidates(name)
        cid = next((cand_index[c] for c in cands if c in cand_index), None)
        if not cid:                                      # fall back: containment
            n = norm(name)
            for c, ocid in cand_index.items():
                if len(c) >= 8 and (n in c or c in n):
                    cid = ocid
                    break
        if cid and cid not in used:
            matches[cid] = name
            used.add(cid)
    return matches


def build():
    log("Parsing XML ...")
    root = ET.parse(XML_PATH).getroot()
    items = root.findall("item")
    by_id = {text(i, "content_id"): i for i in items}

    # Index children by their parent structure, and roles by the structure
    # they belong to.
    children_of = defaultdict(list)      # parent content_id -> [substructure items]
    roles_of = defaultdict(list)         # structure content_id -> [role items]
    portfolios = {}                      # content_id -> title
    orgs = []

    for i in items:
        ty = text(i, "type")
        if ty == "portfolio":
            portfolios[text(i, "content_id")] = text(i, "title")
        elif ty == "organisation":
            orgs.append({
                "content_id": text(i, "content_id"),
                "title": text(i, "title"),
                "acronym": text(i, "acronym"),
                "org_acronym": text(i, "org_acronym"),
            })
        elif ty == "directory_sub_structure":
            children_of[text(i, "parent_directory_structure")].append(i)
        elif ty in ROLE_TYPES:
            roles_of[text(i, "role_belongs_to")].append(i)

    def role_dict(r):
        contact = text(r, "contact")
        person = by_id.get(contact)
        pname = text(person, "title") if person is not None else ""
        vacant = text(r, "vacant").upper() == "TRUE"
        imp = text(r, "importance")
        return {
            "role": text(r, "title"),
            "person": "" if vacant else pname,
            "vacant": vacant,
            "acting": text(r, "is_acting_current").upper() == "TRUE",
            "email": text(r, "email"),
            "phone": text(r, "phone_number"),
            "importance": int(imp) if imp.isdigit() else 999,
        }

    def build_node(cid, seen):
        if cid in seen:                                  # cycle guard
            return None
        seen = seen | {cid}
        item = by_id.get(cid)
        roles = sorted((role_dict(r) for r in roles_of.get(cid, [])),
                       key=lambda x: x["importance"])
        kids = []
        for sub in sorted(children_of.get(cid, []), key=lambda s: text(s, "title")):
            node = build_node(text(sub, "content_id"), seen)
            if node:
                kids.append(node)
        return {
            "content_id": cid,
            "title": text(item, "title") if item is not None else "",
            "roles": roles,
            "children": kids,
        }

    def count_people(node):
        n = sum(1 for r in node["roles"] if r["person"])
        for c in node["children"]:
            n += count_people(c)
        return n

    agencies = load_dashboard_agencies()
    dash_match = match_to_dashboard(orgs, agencies)
    log(f"Register organisations: {len(orgs)} | dashboard agencies: {len(agencies)} "
        f"| matched: {len(dash_match)}")

    OUT_DIR.mkdir(exist_ok=True)
    catalogue = []
    for o in orgs:
        cid = o["content_id"]
        item = by_id[cid]
        tree = build_node(cid, set())
        dashboard_name = dash_match.get(cid)
        slug = slugify(dashboard_name or o["title"])
        chart = {
            "content_id": cid,
            "title": o["title"],
            "acronym": o.get("org_acronym") or o.get("acronym"),
            "portfolio": portfolios.get(text(item, "portfolio_id"), ""),
            "website": text(item, "website"),
            "description": text(item, "description"),
            "dashboard_name": dashboard_name,
            "people_count": count_people(tree),
            "tree": tree,
        }
        (OUT_DIR / f"{slug}.json").write_text(
            json.dumps(chart, indent=2, ensure_ascii=False), encoding="utf-8")
        catalogue.append({
            "slug": slug,
            "title": o["title"],
            "acronym": chart["acronym"],
            "portfolio": chart["portfolio"],
            "people_count": chart["people_count"],
            "in_dashboard": bool(dashboard_name),
            "dashboard_name": dashboard_name,
        })

    catalogue.sort(key=lambda c: c["title"].lower())
    (OUT_DIR / "index.json").write_text(
        json.dumps({"count": len(catalogue), "agencies": catalogue},
                   indent=2, ensure_ascii=False), encoding="utf-8")

    unmatched = [a for a in agencies if a not in dash_match.values()]
    log(f"Wrote {len(catalogue)} charts -> {OUT_DIR.name}/")
    log(f"Dashboard agencies with no register match: {len(unmatched)}")
    return catalogue, unmatched


def main():
    ap = argparse.ArgumentParser(description="Scrape AU gov org charts from directory.gov.au")
    ap.add_argument("--refresh", action="store_true", help="re-download the XML export")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.refresh or not XML_PATH.exists():
        download_xml()
    cat, unmatched = build()
    if not args.quiet:
        log("\nTop agencies by people listed:")
        for c in sorted(cat, key=lambda x: -x["people_count"])[:10]:
            log(f"  {c['people_count']:4}  {c['title']}")
        if unmatched:
            log("\nUnmatched dashboard agencies (first 15):")
            for a in unmatched[:15]:
                log(f"  - {a}")


if __name__ == "__main__":
    main()
