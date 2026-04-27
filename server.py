#!/usr/bin/env python3
"""
ApplyUSA API Server
===================
Serves the ApplyUSA website + a JSON API backed by College Scorecard data
and Claude-scraped deadline information.

Endpoints:
  GET  /                              → index.html
  GET  /api/universities              → paginated, filtered university list
  GET  /api/stats                     → summary counts & state list
  POST /api/refresh                   → trigger agent re-scrape in background

Query params for /api/universities:
  search      partial match on name / city / state
  state       2-letter state code (e.g. CA, NY)
  type        "Public" | "Private Nonprofit" | "Private For-Profit"
  has_deadlines  "true" to show only schools with deadline data
  hbcu        "true" to filter HBCUs
  sort        name | acceptance_rate | size | deadline  (default: name)
  page        page number (default: 1)
  limit       results per page (default: 24, max: 100)

Usage:
  python3 server.py
  # then open http://localhost:5000
"""

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import json
import threading
import subprocess
import os
from pathlib import Path
from datetime import datetime, date

ROOT      = Path(__file__).parent
DATA_DIR  = ROOT / "data"
UNI_FILE  = DATA_DIR / "universities.json"
SEED_FILE = DATA_DIR / "deadlines_seed.json"

app = Flask(__name__, static_folder=str(ROOT))
CORS(app)

_cache: dict = {}
_cache_time: datetime | None = None
_CACHE_TTL = 60  # seconds


# ── Data Loading ──────────────────────────────────────────────────────────────
def load_data() -> dict:
    global _cache, _cache_time
    now = datetime.now()
    if _cache and _cache_time and (now - _cache_time).seconds < _CACHE_TTL:
        return _cache

    if UNI_FILE.exists():
        with open(UNI_FILE) as f:
            _cache = json.load(f)
    elif SEED_FILE.exists():
        # Fall back to seed data only
        with open(SEED_FILE) as f:
            seed = json.load(f)
        unis = []
        for u in seed.get("universities", []):
            unis.append({
                "id":               None,
                "name":             u.get("name"),
                "city":             u.get("city", ""),
                "state":            u.get("state", ""),
                "zip":              "",
                "website":          u.get("website", ""),
                "type":             u.get("type", ""),
                "acceptance_rate":  u.get("acceptance_rate"),
                "student_size":     u.get("student_size"),
                "tuition_in_state": u.get("tuition_in_state"),
                "tuition_out":      u.get("tuition_out"),
                "is_hbcu":          u.get("is_hbcu", False),
                "is_hsi":           u.get("is_hsi", False),
                "deadlines":        u.get("deadlines"),
                "has_deadline_data": u.get("deadlines") is not None,
            })
        _cache = {
            "universities": unis,
            "total": len(unis),
            "last_updated": seed.get("metadata", {}).get("last_updated"),
            "cycle": seed.get("metadata", {}).get("cycle", "2026-2027"),
            "source": "seed",
        }
    else:
        _cache = {"universities": [], "total": 0, "last_updated": None, "cycle": "2026-2027"}

    _cache_time = now
    return _cache


# ── Deadline helpers ──────────────────────────────────────────────────────────
def _earliest_ug_deadline(u: dict) -> str:
    dl = u.get("deadlines") or {}
    ug = dl.get("undergraduate") or {}
    dates = [v for v in ug.values() if isinstance(v, str) and len(v) == 10]
    return min(dates) if dates else "9999-12-31"


def _deadline_status(date_str: str | None) -> str:
    if not date_str:
        return "tbd"
    try:
        d = date.fromisoformat(date_str)
        delta = (d - date.today()).days
        if delta < 0:
            return "closed"
        if delta <= 30:
            return "soon"
        return "open"
    except ValueError:
        return "tbd"


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(str(ROOT), "index.html")


@app.route("/data/<path:filename>")
def static_data(filename):
    return send_from_directory(str(DATA_DIR), filename)


@app.route("/api/universities")
def get_universities():
    data        = load_data()
    universities = list(data.get("universities", []))

    # ── Filters ──
    search       = request.args.get("search",       "").lower().strip()
    state        = request.args.get("state",        "").upper().strip()
    uni_type     = request.args.get("type",         "").strip()
    has_deadlines = request.args.get("has_deadlines", "")
    hbcu         = request.args.get("hbcu",         "")
    dl_status    = request.args.get("status",       "")  # open | soon | closed | tbd
    sort_by      = request.args.get("sort",         "name")
    page         = max(1, int(request.args.get("page",  1)))
    limit        = min(100, max(1, int(request.args.get("limit", 24))))

    if search:
        universities = [
            u for u in universities
            if search in u.get("name",  "").lower()
            or search in u.get("city",  "").lower()
            or search in u.get("state", "").lower()
        ]
    if state:
        universities = [u for u in universities if u.get("state", "").upper() == state]
    if uni_type:
        universities = [u for u in universities if uni_type.lower() in u.get("type", "").lower()]
    if has_deadlines == "true":
        universities = [u for u in universities if u.get("has_deadline_data")]
    if hbcu == "true":
        universities = [u for u in universities if u.get("is_hbcu")]
    if dl_status:
        universities = [
            u for u in universities
            if _deadline_status(_earliest_ug_deadline(u)) == dl_status
        ]

    # ── Sort ──
    if sort_by == "acceptance_rate":
        universities.sort(key=lambda u: u.get("acceptance_rate") or 100)
    elif sort_by == "size":
        universities.sort(key=lambda u: u.get("student_size") or 0, reverse=True)
    elif sort_by == "deadline":
        universities.sort(key=_earliest_ug_deadline)
    else:
        universities.sort(key=lambda u: u.get("name", "").lower())

    # ── Paginate ──
    total  = len(universities)
    start  = (page - 1) * limit
    page_data = universities[start : start + limit]

    # Annotate each with deadline status
    for u in page_data:
        u["deadline_status"] = _deadline_status(_earliest_ug_deadline(u))

    return jsonify({
        "universities": page_data,
        "total":        total,
        "page":         page,
        "limit":        limit,
        "pages":        max(1, (total + limit - 1) // limit),
        "last_updated": data.get("last_updated"),
        "cycle":        data.get("cycle"),
        "source":       data.get("source", "live"),
    })


@app.route("/api/stats")
def get_stats():
    data         = load_data()
    universities = data.get("universities", [])

    state_counts: dict[str, int] = {}
    open_count = soon_count = closed_count = tbd_count = 0

    for u in universities:
        s = u.get("state", "")
        state_counts[s] = state_counts.get(s, 0) + 1
        status = _deadline_status(_earliest_ug_deadline(u))
        if status == "open":   open_count   += 1
        elif status == "soon": soon_count   += 1
        elif status == "closed": closed_count += 1
        else:                  tbd_count    += 1

    return jsonify({
        "total":         len(universities),
        "with_deadlines": sum(1 for u in universities if u.get("has_deadline_data")),
        "public":        sum(1 for u in universities if "Public" in u.get("type", "")),
        "private":       sum(1 for u in universities if "Private" in u.get("type", "")),
        "hbcu":          sum(1 for u in universities if u.get("is_hbcu")),
        "open":          open_count,
        "soon":          soon_count,
        "closed":        closed_count,
        "tbd":           tbd_count,
        "states":        sorted(k for k in state_counts if k),
        "last_updated":  data.get("last_updated"),
        "source":        data.get("source", "live"),
    })


@app.route("/api/refresh", methods=["POST"])
def refresh():
    global _cache, _cache_time
    _cache = {}
    _cache_time = None

    def _run_agent():
        subprocess.run(
            ["python3", "agent.py", "--top", "500", "--scrape-deadlines"],
            cwd=str(ROOT),
        )
        global _cache, _cache_time
        _cache = {}
        _cache_time = None

    threading.Thread(target=_run_agent, daemon=True).start()
    return jsonify({
        "status":  "started",
        "message": "Agent running in background — check /api/stats in a few minutes.",
    })


# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print()
    print("  ApplyUSA API Server")
    print(f"  Website  -> http://localhost:{port}")
    print()
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
