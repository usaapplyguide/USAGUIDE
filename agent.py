#!/usr/bin/env python3
"""
ApplyUSA Deadline Scraper Agent
================================
Fetches ALL accredited US universities from the Dept. of Education
College Scorecard API, then uses Claude AI to extract application
and scholarship deadlines from each university's admissions page.

Usage:
  python3 agent.py                            # Full run, seed data only
  python3 agent.py --top 500                  # Limit to 500 universities
  python3 agent.py --scrape-deadlines         # Enable Claude web scraping
  python3 agent.py --university "MIT"         # Refresh one university
  python3 agent.py --top 200 --scrape-deadlines  # Recommended first run

Requirements:
  ANTHROPIC_API_KEY env var (for --scrape-deadlines)
  COLLEGE_SCORECARD_KEY env var (optional; DEMO_KEY is rate-limited)
  pip install -r requirements.txt
"""

import anthropic
import requests
import json
import os
import sys
import time
import argparse
import logging
from datetime import datetime
from bs4 import BeautifulSoup
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

UNIVERSITIES_FILE = DATA_DIR / "universities.json"
SEED_FILE         = DATA_DIR / "deadlines_seed.json"

# ── Config ───────────────────────────────────────────────────────────────────
SCORECARD_KEY = os.getenv("COLLEGE_SCORECARD_KEY", "DEMO_KEY")
SCORECARD_URL = "https://api.data.gov/ed/collegescorecard/v1/schools.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ApplyUSA-Bot/1.0; "
        "educational resource aggregator)"
    )
}

SCORECARD_FIELDS = ",".join([
    "id", "ope8_id",
    "school.name", "school.city", "school.state", "school.zip",
    "school.school_url", "school.ownership", "school.locale",
    "school.degrees_awarded.predominant",
    "school.degrees_awarded.highest",
    "school.minority_serving.historically_black",
    "school.minority_serving.hispanic",
    "latest.admissions.admission_rate.overall",
    "latest.student.size",
    "latest.cost.tuition.in_state",
    "latest.cost.tuition.out_of_state",
])

OWNERSHIP = {1: "Public", 2: "Private Nonprofit", 3: "Private For-Profit"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("applyusa")


# ── College Scorecard API ─────────────────────────────────────────────────────
def fetch_all_universities(limit: int | None = None) -> list[dict]:
    """Paginate through the College Scorecard API and return all 4-year schools."""
    log.info("Connecting to College Scorecard API …")
    universities: list[dict] = []
    page = 0
    per_page = 100

    base_params = {
        "api_key": SCORECARD_KEY,
        "school.degrees_awarded.highest__range": "3..4",
        "school.operating": 1,
        "fields": SCORECARD_FIELDS,
        "per_page": per_page,
    }

    while True:
        try:
            resp = requests.get(
                SCORECARD_URL, params={**base_params, "page": page}, timeout=30
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            log.error("API error on page %d: %s", page, exc)
            break

        results = payload.get("results", [])
        if not results:
            break

        for school in results:
            universities.append(_normalize(school))

        total = payload.get("metadata", {}).get("total", "?")
        log.info("  page %3d — %d / %s fetched", page, len(universities), total)

        if limit and len(universities) >= limit:
            universities = universities[:limit]
            break
        if len(results) < per_page:
            break

        page += 1
        time.sleep(0.25)

    log.info("Fetched %d universities from College Scorecard", len(universities))
    return universities


def _normalize(s: dict) -> dict:
    ar = s.get("latest.admissions.admission_rate.overall")
    return {
        "id":               s.get("id"),
        "name":             s.get("school.name", "Unknown"),
        "city":             s.get("school.city", ""),
        "state":            s.get("school.state", ""),
        "zip":              s.get("school.zip", ""),
        "website":          s.get("school.school_url", ""),
        "type":             OWNERSHIP.get(s.get("school.ownership"), "Unknown"),
        "acceptance_rate":  round(ar * 100, 1) if ar else None,
        "student_size":     s.get("latest.student.size"),
        "tuition_in_state": s.get("latest.cost.tuition.in_state"),
        "tuition_out":      s.get("latest.cost.tuition.out_of_state"),
        "is_hbcu":          bool(s.get("school.minority_serving.historically_black")),
        "is_hsi":           bool(s.get("school.minority_serving.hispanic")),
        "predominant_deg":  s.get("school.degrees_awarded.predominant"),
        "highest_deg":      s.get("school.degrees_awarded.highest"),
    }


# ── Web Scraping ──────────────────────────────────────────────────────────────
ADMISSIONS_PATHS = [
    "/admissions", "/admission", "/apply", "/undergraduate-admissions",
    "/future-students", "/prospective-students", "/admissions/apply",
    "/undergraduate/admissions",
]

def fetch_admissions_text(website: str) -> str | None:
    """Try common admissions URL paths and return cleaned page text."""
    if not website:
        return None
    base = website.rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base

    for path in ADMISSIONS_PATHS:
        url = base + path
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
            if resp.status_code == 200 and len(resp.text) > 800:
                soup = BeautifulSoup(resp.text, "lxml")
                for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe"]):
                    tag.decompose()
                text = soup.get_text(separator=" ", strip=True)
                # Keep first 6 000 chars — enough for Claude, cheap on tokens
                return text[:6000]
        except Exception:
            continue
    return None


# ── Claude Extraction ─────────────────────────────────────────────────────────
_PROMPT = """\
You are an expert at reading university admissions pages and extracting deadline dates.

University: {name}
Application cycle: 2026-2027 (enrollment Fall 2027)

--- PAGE CONTENT START ---
{content}
--- PAGE CONTENT END ---

Return ONLY a valid JSON object with this structure (no explanation, no markdown):
{{
  "undergraduate": {{
    "early_action":      "YYYY-MM-DD or null",
    "early_decision_1":  "YYYY-MM-DD or null",
    "early_decision_2":  "YYYY-MM-DD or null",
    "regular_decision":  "YYYY-MM-DD or null",
    "rolling":           false
  }},
  "graduate": {{
    "ms_fall":           "YYYY-MM-DD or null",
    "phd_fall":          "YYYY-MM-DD or null",
    "mba":               "YYYY-MM-DD or null",
    "rolling":           false,
    "varies_by_dept":    false
  }},
  "scholarship": {{
    "merit_priority":         "YYYY-MM-DD or null",
    "financial_aid_priority": "YYYY-MM-DD or null",
    "css_profile":            "YYYY-MM-DD or null",
    "fafsa_priority":         "YYYY-MM-DD or null"
  }},
  "notes": "one sentence about any special deadline rules, or empty string"
}}

Rules:
- Use null for any date not found in the content
- Only use dates explicitly stated — do not guess
- If year is ambiguous assume 2026 for fall/early deadlines, 2027 for spring/RD
"""


def extract_with_claude(client: anthropic.Anthropic, name: str, content: str) -> dict | None:
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=700,
            messages=[{"role": "user", "content": _PROMPT.format(name=name, content=content)}],
        )
        text = msg.content[0].text.strip()
        start, end = text.find("{"), text.rfind("}") + 1
        if 0 <= start < end:
            return json.loads(text[start:end])
    except Exception as exc:
        log.warning("Claude extraction failed for %s: %s", name, exc)
    return None


# ── Seed Merge ────────────────────────────────────────────────────────────────
def load_seed() -> dict[str, dict]:
    if not SEED_FILE.exists():
        return {}
    with open(SEED_FILE) as f:
        seed = json.load(f)
    return {d["name"].lower(): d for d in seed.get("universities", [])}


def match_seed(name: str, seed_map: dict) -> dict | None:
    low = name.lower()
    if low in seed_map:
        return seed_map[low]
    for key, val in seed_map.items():
        if key in low or low in key:
            return val
    return None


# ── Runner ────────────────────────────────────────────────────────────────────
def run(top: int | None, scrape: bool, university_filter: str | None) -> None:
    client = anthropic.Anthropic() if scrape else None
    seed_map = load_seed()

    universities = fetch_all_universities(limit=top)

    results: list[dict] = []
    seed_hits = scrape_hits = 0

    for i, uni in enumerate(universities):
        if university_filter and university_filter.lower() not in uni["name"].lower():
            uni["deadlines"] = None
            uni["has_deadline_data"] = False
            results.append(uni)
            continue

        dl = None

        # 1. Try pre-seeded data
        seed = match_seed(uni["name"], seed_map)
        if seed:
            dl = seed.get("deadlines")
            seed_hits += 1

        # 2. Scrape with Claude if still missing
        if scrape and not dl and uni.get("website"):
            log.info("[%d/%d] Scraping %s …", i + 1, len(universities), uni["name"])
            text = fetch_admissions_text(uni["website"])
            if text:
                dl = extract_with_claude(client, uni["name"], text)
                if dl:
                    scrape_hits += 1
            time.sleep(1.5)  # polite delay

        uni["deadlines"] = dl
        uni["has_deadline_data"] = dl is not None
        results.append(uni)

    log.info("Seed matches: %d  |  Scraped: %d", seed_hits, scrape_hits)

    output = {
        "last_updated": datetime.now().isoformat(),
        "cycle":        "2026-2027",
        "total":        len(results),
        "universities": results,
    }
    with open(UNIVERSITIES_FILE, "w") as f:
        json.dump(output, f, indent=2)
    log.info("Saved %d universities → %s", len(results), UNIVERSITIES_FILE)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ApplyUSA Deadline Scraper Agent")
    parser.add_argument("--top",              type=int,  default=None, help="Limit to top N universities")
    parser.add_argument("--scrape-deadlines", action="store_true",     help="Scrape with Claude (needs ANTHROPIC_API_KEY)")
    parser.add_argument("--university",       type=str,  default=None, help="Filter to a specific university name")
    args = parser.parse_args()
    run(top=args.top, scrape=args.scrape_deadlines, university_filter=args.university)
