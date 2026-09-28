"""
Priority Engine — Layer 3 of the RoleReach processing pipeline.

Scores each ELIGIBLE/REVIEW job with a Priority Score (0–10) and Level
(HIGH/MEDIUM/LOW) based on three weighted factors:

    Priority Score = (Fit × 0.60) + (Freshness × 0.25) + (Access × 0.15)

Fit      — Overall Fit Score from Layer 2 (falls back to Role Fit score while
           Overall Fit weighting is pending).
Freshness — Derived from the job's posted_at; uses existing scraper data.
Access   — Actionability: named person + email → 3pts, named person only → 2pts,
           unattributed email or job link → 1pt, nothing → 0pts.
"""

import re
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Freshness scoring
# ---------------------------------------------------------------------------

_HOURS_RE = re.compile(r"(\d+)\s*(?:hour|hr)s?\s+ago", re.IGNORECASE)
_MINS_RE = re.compile(r"(\d+)\s*(?:minute|min)s?\s+ago", re.IGNORECASE)
_DAYS_RE = re.compile(r"(\d+)\s*days?\s+ago", re.IGNORECASE)
_WEEKS_RE = re.compile(r"(\d+)\s*weeks?\s+ago", re.IGNORECASE)
_MONTHS_RE = re.compile(r"(\d+)\s*months?\s+ago", re.IGNORECASE)

_ISO_FORMATS = [
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
]


def _days_since(posted_at):
    """Return days since posting, or None if unparseable."""
    if not posted_at:
        return None

    text = str(posted_at).strip()
    lower = text.lower()

    if "just now" in lower or "moments ago" in lower:
        return 0
    if "today" in lower:
        return 0
    if "yesterday" in lower:
        return 1

    m = _MINS_RE.search(lower) or _HOURS_RE.search(lower)
    if m:
        return 0

    m = _DAYS_RE.search(lower)
    if m:
        return int(m.group(1))

    m = _WEEKS_RE.search(lower)
    if m:
        return int(m.group(1)) * 7

    m = _MONTHS_RE.search(lower)
    if m:
        return int(m.group(1)) * 30

    # ISO / date formats — normalize then try each pattern
    # Strip trailing Z, replace T separator with a space for strptime compatibility
    clean = text.rstrip("Zz").replace("T", " ").replace("t", " ")
    for fmt in _ISO_FORMATS:
        try:
            dt = datetime.strptime(clean[: len(fmt) + 3], fmt)
            return max(0, (datetime.now() - dt).days)
        except ValueError:
            continue

    # Last resort: Python's fromisoformat (handles offsets in 3.7+)
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is not None:
            now = datetime.now(timezone.utc)
        else:
            now = datetime.now()
        return max(0, (now - dt).days)
    except (ValueError, TypeError, AttributeError):
        pass

    return None


def _freshness_score(job):
    """0–10 freshness score from posted_at. Unparseable defaults to mid-bucket (4)."""
    days = _days_since(job.get("posted_at"))
    if days is None:
        return 4  # conservative mid-bucket
    if days <= 1:
        return 10
    if days <= 3:
        return 7
    return 4  # 4–7 days (eligibility gate already blocked older jobs)


# ---------------------------------------------------------------------------
# Access scoring
# ---------------------------------------------------------------------------

def _access_points(job):
    """
    Return access points (0–3) based on how actionable the opportunity is.

    3 — Named relevant person + direct email
    2 — Named relevant person identified (no email: LinkedIn/other)
    1 — Unattributed email OR company LinkedIn page OR application URL only
    0 — No usable contact route
    """
    hm_name = (job.get("hm_name") or "").strip()
    hm_email = (job.get("hm_email") or "").strip()
    company_linkedin = (job.get("company_linkedin") or "").strip()
    url = (job.get("url") or "").strip()

    if hm_name and hm_email:
        return 3
    if hm_name:
        return 2  # person identified but no direct email
    if hm_email or company_linkedin or url:
        return 1  # unattributed email, company LinkedIn, or application link
    return 0


def _access_score(points):
    """Normalize access points (0–3) to a 0–10 score."""
    return round((points / 3) * 10, 1)


# ---------------------------------------------------------------------------
# Fit input selection
# ---------------------------------------------------------------------------

def _fit_score_for_priority(job):
    """
    Return the fit score to use in the priority calculation.

    Prefers overall_fit_score (fully weighted Layer 2 result).
    Falls back to role_fit_score while overall_fit weighting is pending.
    Returns None only when neither is available (fit assessment hasn't run).
    """
    overall = job.get("overall_fit_score")
    if overall is not None:
        return float(overall)
    role = job.get("role_fit_score")
    if role is not None:
        return float(role)
    return None


# ---------------------------------------------------------------------------
# Priority level mapping
# ---------------------------------------------------------------------------

def _priority_level(score):
    if score >= 8.0:
        return "HIGH"
    if score >= 5.0:
        return "MEDIUM"
    return "LOW"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calculate_priority(job):
    """
    Compute the priority score for one job dict.

    Returns a dict with:
        priority_score          REAL  — weighted composite (0–10), None if fit pending
        priority_level          TEXT  — HIGH / MEDIUM / LOW / PENDING
        priority_fit_score_used REAL  — the fit score fed into the formula
        priority_freshness_score REAL — freshness component (0–10)
        priority_access_points  INT   — 0, 1, 2, or 3
        priority_access_score   REAL  — access component (0–10)
    """
    fit = _fit_score_for_priority(job)
    freshness = _freshness_score(job)
    pts = _access_points(job)
    access = _access_score(pts)

    if fit is None:
        return {
            "priority_score": None,
            "priority_level": "PENDING",
            "priority_fit_score_used": None,
            "priority_freshness_score": float(freshness),
            "priority_access_points": pts,
            "priority_access_score": access,
        }

    score = round((fit * 0.60) + (freshness * 0.25) + (access * 0.15), 1)
    return {
        "priority_score": score,
        "priority_level": _priority_level(score),
        "priority_fit_score_used": fit,
        "priority_freshness_score": float(freshness),
        "priority_access_points": pts,
        "priority_access_score": access,
    }
