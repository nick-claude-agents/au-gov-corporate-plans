"""
Corporate Plan BD Analyser
==========================

Reads an Australian Government corporate plan (PDF or web page), sends it to
Claude, and produces a business-development analysis for the Consulting Firm:
the agency's strategic priorities, reform drivers and pressures, where they map
to the Consulting Firm's services, and concrete recommended actions.

Output:
  * Structured JSON  (machine-readable; saved and/or printed)
  * An HTML snippet   (styled to match the daily Corporate Plans email)

Designed to be called by Check-CorporatePlans.ps1 whenever a NEW or UPDATED
plan is detected, but also usable standalone from the command line.

Usage (standalone):
  python analyse_corporate_plan.py \
      --agency "Department of Veterans' Affairs (DVA)" \
      --portfolio "Veterans' Affairs" \
      --url "https://www.dva.gov.au/.../corporate-plan-2025-26.pdf" \
      --json-out dva.json --html-out dva.html

  # Validate extraction + prompt without calling the API (no tokens spent):
  python analyse_corporate_plan.py --agency X --portfolio Y --url ... --dry-run

Requires: anthropic, pdfplumber, beautifulsoup4, requests
Set ANTHROPIC_API_KEY in the environment or in the .env file alongside this script.
"""

import argparse
import io
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import List, Literal, Optional

import requests

# Third-party parsers imported lazily inside functions so --help works without them.

# ── Configuration ──────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
CONFIG = {
    "model": "claude-opus-4-8",
    "max_tokens": 12000,
    "log_file": str(SCRIPT_DIR / "plan_analyser.log"),
    "request_timeout": 75,          # seconds for downloading the plan (gov hosts can be slow)
    "download_attempts": 3,         # retry slow/flaky plan downloads
    "max_chars": 600_000,           # safety cap on plan text (~150K tokens)
    "research_max_searches": 8,     # cap web searches per plan (cost control)
    "research_max_turns": 6,        # bounded pause_turn loop for the research call
    "max_retries": 8,               # SDK retries (rides out 429s on low tiers)
    # Use a real browser User-Agent + Accept headers. Many gov sites time out or
    # return 403 on a non-browser UA (e.g. one containing "Analyser"/"bot"),
    # which was causing the backfill's plan downloads to fail.
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "application/pdf,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
    },
}

# ── Firm profile ───────────────────────────────────────────────────────────────
# EDIT THIS to reflect the Consulting Firm's actual service lines and positioning. It is the
# single most important input to the quality of the analysis: Claude maps the
# agency's needs onto exactly these services, so keep the list accurate and
# specific. (It is also large on purpose — a >4K-token system prefix is what lets
# prompt caching kick in on Opus; see the note in build_system_prompt.)
FIRM_PROFILE = """\
The Consulting Firm is an independent professional services firm that works
almost exclusively with Australian Government (Commonwealth) entities. It is
engaged by departments, agencies and statutory authorities across the full
lifecycle — strategy and planning, program and project delivery, assurance and
audit, corporate and financial functions, workforce, and digital/ICT delivery —
and holds positions on Commonwealth panels spanning management consulting and
digital services.

Service lines (map agency needs ONLY onto these; use the exact group name):

1. Financial & Accounting
   - Financial accounting, budgeting and general financial services
   - Accounting advisory and financial advisory
   - Financial assessments, costing and financial-sustainability advice

2. Audit & Assurance
   - Financial audit, performance audit, and combined financial-and-performance audits
   - Independent assurance, health checks and quality/confirmation reviews
   - Quality management

3. Procurement, Probity & Contracts
   - Strategic procurement advice and procurement/acquisition support
   - Probity advisory services (non-legal) and probity auditing
   - Contract management; commercial advice; digital sourcing and contract management

4. Programs, Projects & Change
   - Program and project management and delivery
   - Business change implementation and change management
   - Program/project/change management for corporate and digital initiatives

5. Strategy, Policy & Governance
   - Business strategy and planning; technical and information strategy
   - Corporate governance and governance frameworks
   - Strategy, policy and governance advisory; advice and guidance

6. Organisation, Workforce & People
   - Organisational planning and development; capability and performance
   - Workforce management and human resources; people and skill management
   - Training, learning and development

7. Data, Analytics & Information
   - Data analytics and management; business intelligence
   - Information strategy and information management
   - Research; market research and advisory

8. Digital & ICT Delivery
   - Systems development; solutions implementation; installation and integration
   - User experience and service design; service transition and operation
   - Support and operations; business, systems and process analysis

9. Cybersecurity
   - Cybersecurity advisory and services
   - Security assessment and assurance for systems and data

10. Communications & Engagement
   - Community and stakeholder engagement; communications
   - Marketing, advertising, communications and engagement
   - Content and publishing; authoring and writing services

Typical engagement shapes (use these to keep suggested_offering realistic and
proportionate to the agency's size and budget):
- Short advisory / review: 4-10 weeks, 1-2 senior advisers, a findings-and-
  recommendations report (e.g. a health check, financial-sustainability review,
  governance assessment, probity plan).
- Framework / design piece: 6-16 weeks, a small team, a designed artefact and
  rollout support (e.g. target operating model, costing model, performance
  framework, risk framework).
- Embedded delivery / assurance: months to multi-year, embedded resources or a
  rolling assurance role on a named program (e.g. a program/PMO lead, an
  independent assurer attending gateways).
- Evaluation: 8-20 weeks, an evaluation team, an evaluation plan then a report.
- Digital / ICT delivery: scaled to the system or service — discovery, design,
  build/implementation, integration, and ongoing support and operations.
Right-size to the entity: a small collecting institution warrants a short
advisory; a large service-delivery transformation can sustain embedded delivery,
assurance, or systems implementation over years.

Positioning: a broad, independent, Commonwealth-focused provider spanning
management and corporate advisory through to digital/ICT delivery and
cybersecurity. Unlike pure advisory firms, it can BUILD and implement (systems
development, solutions implementation, support and operations), not only advise.
Probity and assurance work is non-legal — the firm is NOT a law firm and does
not provide legal advice. Map each opportunity to the single best-fit service
line above; where two genuinely apply, name a primary and a secondary. Do not
stretch a need onto a service not listed.
"""

# ── Analysis framework (the rubric Claude follows) ─────────────────────────────
ANALYSIS_FRAMEWORK = """\
You are a senior business-development analyst at the Consulting Firm. Your job is
to read an Australian Government entity's CORPORATE PLAN and turn it into a
sharp, honest, actionable BD brief that helps the Consulting Firm decide where and how to
pursue work with this entity over the coming year.

Background you must use:
- Commonwealth entities publish a corporate plan each year under the Public
  Governance, Performance and Accountability Act 2013 (PGPA Act, s35). It is the
  entity's primary planning document and covers: purpose, operating context,
  key activities, strategic priorities, capability, risk oversight, and
  performance measures (usually a 4-year outlook with the budget year detailed).
- A corporate plan is a SIGNAL, not a tender. It tells you what the entity cares
  about, what it is changing, where it is under pressure, and what it is
  investing in. Those signals are where future advisory and delivery work comes
  from. Your task is to read between the lines for *latent demand*.

How to analyse (do all of this from the plan text, grounded in evidence):
1. Identify the entity's genuine strategic priorities for the year — not the
   boilerplate purpose statement, but the things they are actively pushing:
   reforms, new programs, transformations, capability builds, system rollouts,
   regulatory changes, machinery-of-government changes, savings targets.
2. Surface the DRIVERS OF CHANGE and PRESSURE: new legislation or policy, royal
   commission / review responses, growth in demand, workforce gaps, ageing
   systems, integrity/assurance pressure, financial sustainability concerns,
   audit findings. These create consulting demand.
3. For each credible opportunity, map the agency's need to ONE Consulting Firm service
   line (use the exact names from the firm profile). Be specific about the
   offering the Consulting Firm would pitch — name the kind of engagement, not a generic
   "we could help".
4. Be disciplined and honest. If the plan reveals little the Consulting Firm can act on,
   say so. Do NOT invent needs, quote text that isn't there, or stretch to map
   a service onto something it doesn't fit. Mark low-confidence items as such.
5. Recommend concrete next actions a BD lead can take in the next 4-8 weeks:
   who to approach (by role/branch, since plans rarely name individuals), what
   angle to lead with, what to prepare (a capability statement, a thought piece,
   a meeting request), and what to watch for (an upcoming procurement, a review).

Using external research (news, ANAO, Parliament): the user message may include
an EXTERNAL RESEARCH section containing web-search results. Use it ONLY to fill
three sections, and apply strict discipline:
- news_and_controversies: recent media issues, criticism, failures, cost
  blowouts, leadership/governance/culture problems. These are BD signals — a
  troubled program or a critical review often means the agency needs independent
  help.
- anao_findings: relevant ANAO performance-audit findings or recommendations.
  Open recommendations and adverse findings are strong demand signals for
  assurance, governance, program and financial work.
- parliamentary_matters: committee inquiries, Senate Estimates exchanges,
  questions on notice or Hansard bearing on the agency's programs, funding or
  performance.
For each, give a factual summary, a one-line BD implication, and the source.
Include an item ONLY if it appears in the supplied research — never invent or
recall findings from memory, and never put an item without a source. If the
research is absent or a category has nothing credible, return an empty list for
that section. Where a news/ANAO/parliamentary finding strengthens an
opportunity, reference it in that opportunity's rationale too.

Tone: evidence-led, concise, commercially useful, no fluff, no hype. Ground
claims in what the plan actually says. Prefer specific over generic every time.
Where you reference the plan, paraphrase faithfully; only put text in a
source_quote field if it is genuinely close to the plan's wording.

Where corporate plans hide the signals (read these sections closely):
- Purpose & operating context: demand pressures, external change, dependencies.
- Key activities: the real workload — new vs business-as-usual tells you where
  investment and risk sit.
- Strategic priorities / objectives: the deliberate agenda for the period.
- Environment / scanning: legislation, reviews, market and demographic shifts.
- Capability: workforce, systems, data, and ICT gaps the entity admits to.
- Risk oversight: the named enterprise risks are an almost-direct list of where
  independent assurance, governance and program help is wanted.
- Performance measures: weak, vague or newly-introduced measures often signal a
  performance-framework or evaluation need.
- Subsequent-year outlook: multi-year reforms that will generate work over time.

Service-mapping playbook — translate common plan signals into a Consulting Firm
service and a specific offering. Use these as patterns, not a script; only apply
one when the plan genuinely supports it.

- Signal: a major new program, reform response or system rollout is named.
  -> Programs, Projects & Change. Offering: delivery/PMO setup, an embedded
     program manager, a benefits-management framework, or delivery assurance.
- Signal: a Royal Commission, ANAO audit, independent review or integrity
  concern is referenced, or "assurance" appears as a priority.
  -> Audit & Assurance (with Procurement, Probity & Contracts for any probity).
     Offering: independent/gateway-style assurance of the response program, a
     health check, or probity for the procurements that follow.
- Signal: financial sustainability, savings/efficiency targets, cost pressure,
  audit findings, or a new funding/pricing model.
  -> Financial & Accounting (or Audit & Assurance for an audit). Offering: an
     activity-based costing model, a financial-sustainability review, a financial
     or performance audit, or budget/financial-framework uplift.
- Signal: restructure, machinery-of-government change, new operating model,
  "transformation", or a functional review.
  -> Strategy, Policy & Governance with Organisation, Workforce & People.
     Offering: target operating model design, a functional review, governance
     redesign, and change management.
- Signal: new or weak performance measures, an outcomes focus, or a commitment
  to "evaluate" a program.
  -> Strategy, Policy & Governance (with Data, Analytics & Information for
     evidence). Offering: a PGPA-aligned performance-measure redesign, or a
     process/outcome evaluation.
- Signal: governance maturity, committee effectiveness, enterprise risk uplift,
  or internal-audit pressure.
  -> Strategy, Policy & Governance with Audit & Assurance. Offering: a governance
     framework/committee uplift, a risk-framework refresh, or assurance support.
- Signal: large procurements, grants programs, a new approach to market, or
  commercial/contract-management pressure.
  -> Procurement, Probity & Contracts. Offering: procurement strategy and
     evaluation support, probity (non-legal), grants design, or contract-
     management advice.
- Signal: workforce shortages, capability gaps, APS capability reviews, or a
  resourcing/training need.
  -> Organisation, Workforce & People. Offering: workforce planning, a capability
     uplift program, organisational design, or training and development.
- Signal: a technology/data investment, digital transformation, legacy-system
  replacement, a new service/platform, or a user-experience focus.
  -> Digital & ICT Delivery (with Data, Analytics & Information for data work).
     Offering: discovery and service design, systems development or solutions
     implementation, integration, business/process analysis, or ongoing support
     and operations. Unlike pure advisers, the firm can BUILD and implement.
- Signal: cyber risk, an ANAO cyber-resilience audit, a breach, or a
  security-uplift commitment.
  -> Cybersecurity. Offering: cyber advisory, a security assessment, or
     assurance of systems and data.
- Signal: major public-facing change, consultation, campaigns, or stakeholder
  management.
  -> Communications & Engagement. Offering: community and stakeholder engagement,
     communications and campaign support, or content and publishing.
- Signal: data/information growth, analytics ambitions, or research needs.
  -> Data, Analytics & Information. Offering: data analytics and management,
     business intelligence, information strategy, or research.

Worked example (illustrative — adapt to the actual plan):
  Plan signal: "We will stand up a new claims-processing capability and respond
  to the recommendations of the independent review of our service delivery."
  -> Opportunity: delivery + assurance of the claims-processing reform.
     Mapped service: Programs, Projects & Change (primary) with Audit & Assurance
     (secondary). Suggested offering: an embedded delivery lead plus independent
     gateway-style assurance at each stage — and, if a new system underpins it,
     Digital & ICT Delivery to build it. Confidence: High — named program +
     external review create clear, time-bound demand.

Confidence calibration:
- High: the plan names a specific program, reform, review response or pressure
  that maps cleanly to a Consulting Firm service with a plausible budget and timeframe.
- Medium: the need is real but general (e.g. "strengthen governance") with no
  named initiative, so the fit is inferred rather than evidenced.
- Low: you are extrapolating; the plan only hints, or the fit is a stretch. Say
  so plainly rather than overselling.

Engagement targets — plans rarely name individuals. Point to the functions and
roles that own the work: e.g. the relevant Deputy Secretary / Group, the program
or reform branch, the CFO / Chief Operating Officer, the Chief Risk Officer, the
head of the relevant division, or the audit/risk committee. Be as specific as
the plan's structure allows.

Agency archetypes — calibrate the analysis to the kind of entity you are
reading. The same words mean different demand depending on the archetype:

- Large service-delivery agencies (e.g. Services Australia, NDIA, DVA, the ATO):
  high-volume transactional delivery, big technology and claims systems,
  constant demand pressure, frequent reform programs and review responses. Best
  fits are usually Programs, Projects & Change; Digital & ICT Delivery; and
  Audit & Assurance. Watch for named transformation programs and review
  responses — these are the largest, most time-bound opportunities.

- Regulators (e.g. ACMA, APRA, ASIC, TGA, ASQA, AER): smaller, expertise-heavy,
  driven by legislative change and the size of the regulated population. Demand
  shows up as new regulatory functions, data/intelligence capability, and
  performance/assurance of regulatory effectiveness. Best fits: Strategy, Policy
  & Governance; Data, Analytics & Information; and Audit & Assurance. They rarely
  run huge delivery programs.

- Policy departments (e.g. PM&C, Treasury, Finance, DFAT, Home Affairs central):
  policy and stewardship rather than service delivery; demand is around strategy,
  evaluation, financial frameworks, governance and capability. Best fits:
  Strategy, Policy & Governance; Financial & Accounting; Organisation, Workforce
  & People; and Audit & Assurance. Program-delivery opportunities are usually
  about implementing a specific reform the department owns.

- Scientific / research / data agencies (e.g. CSIRO, Geoscience Australia, ABS,
  AIHW, BoM): mission-driven, capital- and data-intensive, often under funding
  pressure and modernising infrastructure. Best fits: Financial & Accounting;
  Digital & ICT Delivery and Data, Analytics & Information; Strategy, Policy &
  Governance; and Audit & Assurance of major infrastructure investments.

- Cultural / collecting institutions (e.g. NLA, NGA, NMA, AWM, Screen Australia,
  Creative Australia): smaller budgets, custodial mandate, digitisation and
  visitor-experience programs, recurring funding-sustainability pressure. Best
  fits: Financial & Accounting; Strategy, Policy & Governance; and Digital & ICT
  Delivery for digitisation. Keep offerings proportionate to a smaller budget.

- Security / intelligence / enforcement (e.g. AFP, ASIO, ASD, AUSTRAC, ABF, ACIC):
  capability- and technology-heavy, secrecy constraints, strong assurance and
  governance expectations. Best fits: Programs, Projects & Change; Cybersecurity;
  Audit & Assurance; and Organisation, Workforce & People. Be realistic about
  clearance and access constraints when recommending engagement.

If you cannot tell the archetype from the plan, infer it from the key activities
and say which archetype you assumed.

PGPA and BD vocabulary you can rely on: "key activities" and "performance
measures" are PGPA-mandated sections; an entity may be a non-corporate
Commonwealth entity (part of the Commonwealth, funded via appropriations) or a
corporate Commonwealth entity (a separate legal body, sometimes with own-source
revenue) — corporate entities under cost pressure are stronger costing /
financial-sustainability prospects. "Enabling" or "corporate" priorities (people,
finance, ICT, governance) are often where independent advisory work is easiest
to win because they are less politically sensitive than program delivery.

What good output looks like: every opportunity is traceable to a line in the
plan, maps to exactly one named Consulting Firm service, and carries an offering a
partner could pitch in a first meeting. What weak output looks like: generic
"we can help with governance", invented programs, or the same boilerplate
applied to every agency. Avoid the latter entirely.
"""

# ── Logging ────────────────────────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    logger = logging.getLogger("plan_analyser")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(CONFIG["log_file"], encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


LOG = setup_logging()


# ── .env loader (zero-dependency, matches the existing project .env) ───────────
def load_env_file() -> None:
    """Populate os.environ from a .env file next to this script (without
    overwriting variables already set in the real environment)."""
    env_path = SCRIPT_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        # Apply if the var is unset OR present-but-empty (an empty env var is
        # effectively unset; a real value already in the environment wins).
        if key and not os.environ.get(key, "").strip():
            os.environ[key] = value


# ── Structured output schema ───────────────────────────────────────────────────
# Imported here so the file fails fast with a clear message if anthropic/pydantic
# are missing, but only when actually building the schema.
def _import_pydantic():
    try:
        from pydantic import BaseModel, Field
    except ImportError:
        LOG.error("pydantic is required (installed with the anthropic SDK). "
                  "Run: pip install anthropic")
        sys.exit(2)
    return BaseModel, Field


def build_models():
    BaseModel, Field = _import_pydantic()

    class StrategicPriority(BaseModel):
        title: str = Field(description="Short name of the priority")
        description: str = Field(
            description="What the agency is actually trying to do this year")
        source_quote: Optional[str] = Field(
            default=None,
            description="Faithful short paraphrase/quote from the plan, if apt")

    class ExternalFinding(BaseModel):
        summary: str = Field(description="What the source actually reports")
        implication: str = Field(
            description="Why it matters for a BD approach to this agency")
        source: Optional[str] = Field(
            default=None, description="Publication/body and URL if available")

    class OpportunityArea(BaseModel):
        area: str = Field(description="The opportunity in a few words")
        agency_need: str = Field(
            description="The underlying need or pressure driving it")
        mapped_service: str = Field(
            description="Exact Consulting Firm service line this maps to")
        suggested_offering: str = Field(
            description="The specific engagement the Consulting Firm would pitch")
        rationale: str = Field(
            description="Why this is credible, grounded in the plan")
        confidence: Literal["High", "Medium", "Low"]

    class RecommendedAction(BaseModel):
        action: str = Field(description="A concrete next step for the BD lead")
        priority: Literal["High", "Medium", "Low"]
        rationale: str = Field(description="Why now, and why it matters")

    class PlanAnalysis(BaseModel):
        agency_name: str
        portfolio: str
        plan_period: str = Field(
            description="Reporting period, e.g. '2025-26', or 'Unknown'")
        executive_summary: str = Field(
            description="3-5 sentence BD-focused summary of the agency this year")
        strategic_priorities: List[StrategicPriority]
        reform_and_change_drivers: List[str] = Field(
            description="Policy, legislative, demand, financial or system drivers")
        challenges_and_risks: List[str] = Field(
            description="Pressures and risks the plan reveals")
        news_and_controversies: List[ExternalFinding] = Field(
            description="Recent news issues/controversies from web research; "
                        "empty list if research found nothing credible")
        anao_findings: List[ExternalFinding] = Field(
            description="Relevant ANAO audit findings from web research; empty "
                        "list if none found")
        parliamentary_matters: List[ExternalFinding] = Field(
            description="Relevant Parliamentary material (inquiries, Estimates, "
                        "QoN, Hansard) from web research; empty list if none")
        opportunity_areas: List[OpportunityArea]
        recommended_actions: List[RecommendedAction]
        engagement_targets: List[str] = Field(
            description="Roles, branches or functions to approach (rarely named "
                        "individuals)")
        talking_points: List[str] = Field(
            description="3-6 sharp talking points to lead a conversation")
        overall_confidence: Literal["High", "Medium", "Low"]
        caveats: str = Field(
            description="Honest caveats: thin plan, weak fit, assumptions made")

    return PlanAnalysis


# ── Plan retrieval & text extraction ───────────────────────────────────────────
def _norm_name(s: str) -> str:
    s = re.sub(r"\(.*?\)", "", (s or "").lower())
    s = re.sub(r"\b(the|department of|office of the|office of)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def portal_plan_url(agency: str) -> Optional[str]:
    """Find this agency's corporate-plan PDF on the Transparency Portal
    (transparency.gov.au, backed by the Kontent.ai delivery API). The portal's
    previewapi.transparency.gov.au asset host does NOT block cloud IPs, so it's a
    reliable fallback when an agency's own website times out / 403s."""
    pid = "80a82ed1-3e33-027b-b7e0-6493f97f18f8"
    api = f"https://deliver.kontent.ai/{pid}/items"
    target = _norm_name(agency)
    skip = 0
    candidates = []          # (year, asset_url) — prefer the most recent year
    try:
        while True:
            r = requests.get(api, headers=CONFIG["http_headers"], timeout=30,
                             params={"system.type": "corp_plan",
                                     "limit": 100, "skip": skip, "depth": 0})
            data = r.json()
            for item in data.get("items", []):
                raw = item["system"]["name"]
                nm = re.sub(r"^\d{4}[-–/]\d{2,4}\s+", "", raw)
                nm = re.sub(r"\s+Corporate Plan.*$", "", nm, flags=re.I).strip()
                n = _norm_name(nm)
                if not n:
                    continue
                if n == target or (len(n) >= 6 and (n in target or target in n)):
                    pdfs = item["elements"].get("pdf_file", {}).get("value", [])
                    if not pdfs:
                        continue
                    cdn = pdfs[0]["url"]
                    m = re.search(r"[a-f0-9-]{36}/([a-f0-9-]{36})/(.+?)(?:\?|$)", cdn)
                    asset = (f"https://previewapi.transparency.gov.au"
                             f"/delivery/assets/{pid}/{m.group(1)}/{m.group(2)}"
                             if m else cdn)
                    ym = re.search(r"(20\d{2})", raw)          # year from the title
                    year = int(ym.group(1)) if ym else 0
                    candidates.append((year, asset))
            if not data.get("pagination", {}).get("next_page", ""):
                break
            skip += 100
    except Exception as e:                       # noqa: BLE001
        LOG.warning("Portal lookup failed for %s: %s", agency, e)
    if candidates:
        candidates.sort(reverse=True)            # newest year first
        return candidates[0][1]
    return None


def fetch_plan_text(url: str, agency: str = "") -> tuple[str, str]:
    """Download the plan and return (extracted_text, source_kind).
    source_kind is 'pdf' or 'html'. If the agency's own URL fails and an agency
    name is supplied, fall back to its Transparency Portal PDF."""
    def _download(u: str):
        last_exc = None
        for attempt in range(1, CONFIG["download_attempts"] + 1):
            try:
                r = requests.get(u, headers=CONFIG["http_headers"],
                                 timeout=CONFIG["request_timeout"],
                                 allow_redirects=True)
                r.raise_for_status()
                return r
            except Exception as e:               # noqa: BLE001 (retry any)
                last_exc = e
                if attempt < CONFIG["download_attempts"]:
                    LOG.warning("Download attempt %d/%d failed (%s); retrying...",
                                attempt, CONFIG["download_attempts"], e)
        raise last_exc

    LOG.info("Downloading plan: %s", url)
    try:
        resp = _download(url)
    except Exception as primary_exc:             # noqa: BLE001
        if agency:
            LOG.warning("Primary URL failed (%s); trying Transparency Portal "
                        "fallback for %s...", primary_exc, agency)
            fb = portal_plan_url(agency)
            if fb and fb != url:
                LOG.info("Portal fallback URL: %s", fb)
                resp = _download(fb)
                url = fb
            else:
                raise
        else:
            raise
    content = resp.content
    content_type = resp.headers.get("Content-Type", "").lower()

    is_pdf = ("application/pdf" in content_type
              or url.lower().endswith(".pdf")
              or content[:5] == b"%PDF-")

    if is_pdf:
        text = extract_pdf_text(content)
        kind = "pdf"
    else:
        text = extract_html_text(resp.text)
        kind = "html"

    text = text.strip()
    if not text:
        raise ValueError(f"No text could be extracted from {url} ({kind})")

    if len(text) > CONFIG["max_chars"]:
        LOG.warning("Plan text is very long (%d chars); truncating to %d.",
                    len(text), CONFIG["max_chars"])
        text = (text[:CONFIG["max_chars"]]
                + "\n\n[... plan truncated for length ...]")
    LOG.info("Extracted %d characters of %s text.", len(text), kind)
    return text, kind


def extract_pdf_text(data: bytes) -> str:
    try:
        import pdfplumber
    except ImportError:
        LOG.error("pdfplumber is required for PDF plans. Run: pip install pdfplumber")
        sys.exit(2)
    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n\n".join(parts)


def extract_html_text(html_text: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        LOG.error("beautifulsoup4 is required for web-page plans. "
                  "Run: pip install beautifulsoup4")
        sys.exit(2)
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "form"]):
        tag.decompose()
    return soup.get_text(separator="\n")


# ── Prompt construction ────────────────────────────────────────────────────────
def build_system_prompt() -> list:
    """The stable, cached system prefix. The cache_control breakpoint on the last
    block caches the whole prefix. Note: Opus only caches prefixes >= ~4096
    tokens — the firm profile + framework are written to comfortably exceed that,
    so repeated runs (e.g. several new plans in one nightly batch) read it from
    cache at ~0.1x cost instead of paying full price each time."""
    text = (
        ANALYSIS_FRAMEWORK
        + "\n\n=== CONSULTING FIRM — FIRM PROFILE ===\n\n"
        + FIRM_PROFILE
        + "\n\nReturn your analysis strictly in the required structured format. "
          "Every opportunity must map to one of the Consulting Firm service lines "
          "named above, using its exact group name. Do not invent needs the plan "
          "does not support."
    )
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def build_user_message(agency: str, portfolio: str, url: str,
                       plan_text: str, source_kind: str,
                       research_text: str = "") -> str:
    research_block = (
        f"\n\n=== EXTERNAL RESEARCH (web search results) BEGINS ===\n"
        f"Use this ONLY to populate news_and_controversies, anao_findings and "
        f"parliamentary_matters. It may be incomplete or partly irrelevant — "
        f"include only items that genuinely matter and keep their sources.\n\n"
        f"{research_text}\n\n"
        f"=== EXTERNAL RESEARCH ENDS ==="
        if research_text.strip() else
        "\n\n(No external research was supplied; leave the news, ANAO and "
        "parliamentary sections as empty lists.)"
    )
    return (
        f"Analyse the following Australian Government corporate plan for "
        f"business-development purposes.\n\n"
        f"Agency: {agency}\n"
        f"Portfolio: {portfolio}\n"
        f"Source URL: {url}\n"
        f"Source type: {source_kind}\n\n"
        f"=== CORPORATE PLAN TEXT BEGINS ===\n\n"
        f"{plan_text}\n\n"
        f"=== CORPORATE PLAN TEXT ENDS ==="
        f"{research_block}"
    )


# ── External research (web search) ─────────────────────────────────────────────
RESEARCH_SYSTEM = (
    "You are a research analyst gathering external intelligence on an Australian "
    "Government entity to support a business-development brief for a public-sector "
    "consultancy. Use the web_search tool to find evidence. Be factual, recent and "
    "source-cited; never speculate or pad. If a category yields little, say so.")


def research_agency(client, agency: str, portfolio: str) -> str:
    """Run web searches for news/controversies, ANAO findings, and Parliamentary
    material on the agency. Returns a plain-text briefing with sources. Returns
    "" on failure so the analysis can still proceed."""
    tools = [{"type": "web_search_20260209", "name": "web_search",
              "max_uses": CONFIG["research_max_searches"]}]
    user = (
        f"Research the Australian Government entity '{agency}' (portfolio: "
        f"{portfolio}). Bias towards the last ~3 years. Search for and report, "
        f"under three clear headings:\n\n"
        f"1. NEWS, ISSUES & CONTROVERSIES — criticism, scandals, failures, cost "
        f"blowouts, leadership/governance problems, service failures, integrity "
        f"or culture issues. Prefer ABC, The Guardian, AFR, The Mandarin, "
        f"Canberra Times, InnovationAus, news.com.au.\n"
        f"2. ANAO — performance audits, audit findings, or recommendations "
        f"involving this entity (search anao.gov.au).\n"
        f"3. PARLIAMENT — committee inquiries, Senate Estimates exchanges, "
        f"questions on notice, or Hansard relevant to the entity's programs, "
        f"funding or performance (search aph.gov.au).\n\n"
        f"For each finding give: a one-line factual summary, a one-line "
        f"implication for a consultancy pursuing work there, and a source "
        f"(publication/body + URL). If a heading yields nothing credible, write "
        f"'Nothing significant found.' Do NOT include anything you did not find "
        f"via search.")

    messages = [{"role": "user", "content": user}]
    resp = None
    container_id = None
    for _ in range(CONFIG["research_max_turns"]):
        kwargs = dict(model=CONFIG["model"], max_tokens=6000,
                      thinking={"type": "adaptive"},
                      system=RESEARCH_SYSTEM, tools=tools, messages=messages)
        # web_search_20260209 uses a code-execution container for dynamic
        # filtering; continuation requests must reference the same container.
        if container_id:
            kwargs["container"] = container_id
        resp = client.messages.create(**kwargs)
        if getattr(resp, "container", None):
            container_id = resp.container.id
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break
    if resp is None:
        return ""
    text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
    searches = getattr(resp.usage, "server_tool_use", None)
    LOG.info("Research done (%d chars; web searches: %s).", len(text),
             getattr(searches, "web_search_requests", "?") if searches else 0)
    return text


# ── Claude call ────────────────────────────────────────────────────────────────
def analyse_with_claude(agency: str, portfolio: str, url: str,
                        plan_text: str, source_kind: str,
                        research: bool = True) -> dict:
    try:
        import anthropic
    except ImportError:
        LOG.error("The anthropic SDK is required. Run: pip install anthropic")
        sys.exit(2)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        LOG.error("ANTHROPIC_API_KEY is not set. Add it to the environment or "
                  "the .env file next to this script.")
        sys.exit(2)

    PlanAnalysis = build_models()
    # Higher max_retries so the SDK rides out 429s on low rate-limit tiers
    # (it honours the retry-after header with exponential backoff).
    client = anthropic.Anthropic(max_retries=CONFIG["max_retries"])

    research_text = ""
    if research:
        LOG.info("Researching %s via web search...", agency)
        try:
            research_text = research_agency(client, agency, portfolio)
        except Exception as e:                  # noqa: BLE001 - degrade gracefully
            LOG.warning("Web research failed (%s); analysing plan only.", e)

    LOG.info("Sending plan to %s for analysis...", CONFIG["model"])
    response = client.messages.parse(
        model=CONFIG["model"],
        max_tokens=CONFIG["max_tokens"],
        thinking={"type": "adaptive"},          # default effort is 'high'
        system=build_system_prompt(),           # cached prefix
        messages=[{
            "role": "user",
            "content": build_user_message(agency, portfolio, url,
                                          plan_text, source_kind, research_text),
        }],
        output_format=PlanAnalysis,
    )

    usage = response.usage
    LOG.info("Tokens — input: %s, cache_read: %s, cache_write: %s, output: %s",
             usage.input_tokens,
             getattr(usage, "cache_read_input_tokens", 0),
             getattr(usage, "cache_creation_input_tokens", 0),
             usage.output_tokens)

    if response.parsed_output is None:
        LOG.error("Model did not return parseable structured output "
                  "(stop_reason=%s).", response.stop_reason)
        sys.exit(3)

    return response.parsed_output.model_dump()


# ── HTML rendering (matches the Corporate Plans email theme) ───────────────────
THEME = {"dark": "#0D3D20", "mid": "#1B6B3A", "pink": "#ffadb5",
         "pink_bg": "#fff3f4", "amber": "#BF360C", "muted": "#666"}


def _esc(s) -> str:
    import html as html_mod
    return html_mod.escape(str(s)) if s is not None else ""


def _conf_badge(level: str) -> str:
    colours = {"High": "#1B6B3A", "Medium": "#BF360C", "Low": "#999"}
    c = colours.get(level, "#999")
    return (f"<span style='background:{c};color:#fff;font-size:11px;"
            f"padding:1px 7px;border-radius:3px'>{_esc(level)}</span>")


def render_html(a: dict) -> str:
    """Render the analysis dict as a self-contained HTML snippet suitable for
    embedding in the daily email (inline styles only)."""
    h: list[str] = []
    h.append(
        f"<div style='border:1px solid #ddd;border-radius:8px;overflow:hidden;"
        f"margin:14px 0;font-family:Arial,sans-serif;color:#222'>")
    # Header
    h.append(
        f"<div style='background:{THEME['dark']};color:#fff;padding:12px 16px'>"
        f"<div style='font-size:16px;font-weight:bold'>{_esc(a['agency_name'])}</div>"
        f"<div style='font-size:12px;opacity:0.85'>{_esc(a['portfolio'])} &middot; "
        f"Corporate Plan {_esc(a['plan_period'])} &middot; "
        f"BD confidence: {_esc(a['overall_confidence'])}</div></div>")
    h.append("<div style='padding:14px 16px'>")

    # Executive summary
    h.append(f"<p style='margin:0 0 12px;font-size:13px;line-height:1.5'>"
             f"{_esc(a['executive_summary'])}</p>")

    def section(title: str) -> None:
        h.append(f"<h4 style='margin:14px 0 6px;color:{THEME['dark']};"
                 f"font-size:13px'>{title}</h4>")

    # Strategic priorities
    if a.get("strategic_priorities"):
        section("Strategic priorities")
        h.append("<ul style='margin:0;padding-left:18px;font-size:13px'>")
        for p in a["strategic_priorities"]:
            h.append(f"<li style='margin-bottom:4px'><strong>{_esc(p['title'])}</strong>"
                     f" — {_esc(p['description'])}</li>")
        h.append("</ul>")

    # Drivers & risks (two compact lists)
    for key, title in [("reform_and_change_drivers", "Reform &amp; change drivers"),
                       ("challenges_and_risks", "Challenges &amp; risks")]:
        if a.get(key):
            section(title)
            h.append("<ul style='margin:0;padding-left:18px;font-size:13px'>")
            for item in a[key]:
                h.append(f"<li style='margin-bottom:3px'>{_esc(item)}</li>")
            h.append("</ul>")

    # External research sections (news / ANAO / Parliament)
    def findings_section(key: str, title: str) -> None:
        items = a.get(key)
        if not items:
            return
        section(title)
        h.append("<ul style='margin:0;padding-left:18px;font-size:13px'>")
        for f in items:
            src = ""
            if f.get("source"):
                s = _esc(f["source"])
                src = (f" <a href='{s}' style='color:{THEME['mid']};font-size:11px'>"
                       f"[source]</a>" if s.startswith("http")
                       else f" <span style='color:{THEME['muted']};font-size:11px'>"
                            f"({s})</span>")
            h.append(f"<li style='margin-bottom:4px'>{_esc(f['summary'])}{src}"
                     f"<div style='color:{THEME['muted']};font-size:11px'>"
                     f"{_esc(f['implication'])}</div></li>")
        h.append("</ul>")

    findings_section("news_and_controversies", "In the news &amp; controversies")
    findings_section("anao_findings", "ANAO audit findings")
    findings_section("parliamentary_matters", "Parliamentary matters")

    # Opportunity areas — the BD heart of the brief, as a table
    if a.get("opportunity_areas"):
        section("Opportunity areas for the Consulting Firm")
        h.append("<table style='width:100%;border-collapse:collapse;font-size:12px'>")
        h.append(f"<tr style='background:{THEME['pink_bg']}'>"
                 f"<th style='padding:6px;text-align:left;border-bottom:2px solid #eee'>Opportunity</th>"
                 f"<th style='padding:6px;text-align:left;border-bottom:2px solid #eee'>Consulting Firm service</th>"
                 f"<th style='padding:6px;text-align:left;border-bottom:2px solid #eee'>Suggested offering</th>"
                 f"<th style='padding:6px;text-align:left;border-bottom:2px solid #eee'>Conf.</th></tr>")
        for o in a["opportunity_areas"]:
            h.append(
                f"<tr style='border-bottom:1px solid #f0f0f0'>"
                f"<td style='padding:6px;vertical-align:top'><strong>{_esc(o['area'])}</strong>"
                f"<div style='color:{THEME['muted']};font-size:11px'>{_esc(o['agency_need'])}</div></td>"
                f"<td style='padding:6px;vertical-align:top'>{_esc(o['mapped_service'])}</td>"
                f"<td style='padding:6px;vertical-align:top'>{_esc(o['suggested_offering'])}"
                f"<div style='color:{THEME['muted']};font-size:11px'>{_esc(o['rationale'])}</div></td>"
                f"<td style='padding:6px;vertical-align:top'>{_conf_badge(o['confidence'])}</td></tr>")
        h.append("</table>")

    # Recommended actions
    if a.get("recommended_actions"):
        section("Recommended actions")
        h.append("<ul style='margin:0;padding-left:18px;font-size:13px'>")
        for r in sorted(a["recommended_actions"],
                        key=lambda x: {"High": 0, "Medium": 1, "Low": 2}.get(x["priority"], 3)):
            h.append(f"<li style='margin-bottom:4px'>{_conf_badge(r['priority'])} "
                     f"{_esc(r['action'])}<div style='color:{THEME['muted']};"
                     f"font-size:11px'>{_esc(r['rationale'])}</div></li>")
        h.append("</ul>")

    # Targets & talking points
    if a.get("engagement_targets"):
        section("Who to approach")
        h.append(f"<p style='margin:0;font-size:13px'>"
                 + ", ".join(_esc(t) for t in a["engagement_targets"]) + "</p>")
    if a.get("talking_points"):
        section("Talking points")
        h.append("<ul style='margin:0;padding-left:18px;font-size:13px'>")
        for t in a["talking_points"]:
            h.append(f"<li style='margin-bottom:3px'>{_esc(t)}</li>")
        h.append("</ul>")

    # Caveats
    if a.get("caveats"):
        h.append(f"<p style='margin:12px 0 0;font-size:11px;color:{THEME['muted']};"
                 f"font-style:italic'>Caveats: {_esc(a['caveats'])}</p>")

    h.append("</div></div>")
    return "".join(h)


# ── CLI ────────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyse an Australian Government corporate plan for "
                    "the Consulting Firm business-development opportunities.")
    parser.add_argument("--agency", required=True, help="Agency name")
    parser.add_argument("--portfolio", required=True, help="Portfolio name")
    parser.add_argument("--url", required=True, help="URL of the corporate plan")
    parser.add_argument("--json-out", help="Path to write the JSON analysis")
    parser.add_argument("--html-out", help="Path to write the HTML snippet")
    parser.add_argument("--print-html", action="store_true",
                        help="Print the HTML snippet to stdout (for the caller "
                             "to capture). Suppresses the JSON stdout print.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and extract the plan, build the prompt, and "
                             "report sizes WITHOUT calling the API.")
    parser.add_argument("--no-research", action="store_true",
                        help="Skip the web-search research step (plan only, "
                             "cheaper and faster).")
    args = parser.parse_args()

    load_env_file()

    try:
        plan_text, source_kind = fetch_plan_text(args.url, args.agency)
    except Exception as e:                       # noqa: BLE001 - report & exit
        LOG.error("Failed to retrieve/extract the plan: %s", e)
        return 1

    if args.dry_run:
        system_chars = len(build_system_prompt()[0]["text"])
        # Dense bulleted instructional text tokenizes at ~3.7 chars/token; chars/4
        # is a conservative floor. Opus needs a >=4096-token prefix to cache.
        low, high = system_chars // 4, int(system_chars / 3.6)
        LOG.info("DRY RUN — no API call made.")
        LOG.info("System prompt: ~%d chars (~%d-%d tokens). Plan text: %d chars "
                 "(~%d tokens).", system_chars, low, high, len(plan_text),
                 len(plan_text) // 4)
        if high < 4096:
            LOG.warning("System prefix is likely under 4096 tokens — prompt "
                        "caching may not activate on Opus. Expand the framework "
                        "if caching matters.")
        else:
            LOG.info("System prefix should exceed Opus's 4096-token cache "
                     "minimum. Confirm on a real run via the cache_read tokens "
                     "logged after the API call.")
        return 0

    try:
        analysis = analyse_with_claude(args.agency, args.portfolio, args.url,
                                       plan_text, source_kind,
                                       research=not args.no_research)
    except Exception as e:                       # noqa: BLE001 - report & exit
        LOG.error("Analysis failed: %s", e)
        return 1

    html_snippet = render_html(analysis)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(analysis, indent=2,
                                                  ensure_ascii=False), encoding="utf-8")
        LOG.info("Wrote JSON to %s", args.json_out)
    if args.html_out:
        Path(args.html_out).write_text(html_snippet, encoding="utf-8")
        LOG.info("Wrote HTML to %s", args.html_out)

    if args.print_html:
        # Only the HTML on stdout, so PowerShell can capture it cleanly.
        sys.stdout.write(html_snippet)
    else:
        print(json.dumps(analysis, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
