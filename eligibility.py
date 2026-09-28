import re

import config
from experience_filter import (
    HARD_CUTOFF_MIN_YEARS,
    has_fresher_signal,
    parse_min_experience,
)

# Titles that should never be rejected on seniority grounds — spec-listed eligible roles
# that happen to contain a keyword from TITLE_EXCLUDE_KEYWORDS (e.g. "staff" in "chief of staff")
_SENIORITY_WHITELIST_PATTERNS = [
    re.compile(r"\bchief\s+of\s+staff\b", re.IGNORECASE),
    re.compile(r"\bassociate\s+product\s+(manager|owner)\b", re.IGNORECASE),
    re.compile(r"\b(junior|jr\.?)\s+(product\s+)?(manager|pm)\b", re.IGNORECASE),
]

# Seniority reject — uses the global list from config plus mid-level from spec
_SENIORITY_REJECT_PATTERNS = [
    re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
    for kw in config.TITLE_EXCLUDE_KEYWORDS
] + [
    re.compile(r"\bmid[- ]level\b", re.IGNORECASE),
]

# PRIMARY_PRODUCT — title patterns for the six eligible APM-tier roles
_PRIMARY_PRODUCT_PATTERNS = [
    re.compile(r"\bassociate\s+product\s+(manager|owner)\b", re.IGNORECASE),
    re.compile(r"\b(junior|jr\.?)\s+(product\s+)?(manager|pm)\b", re.IGNORECASE),
    re.compile(r"\bproduct\s+management\s+intern\b", re.IGNORECASE),
    re.compile(r"\bproduct\s+associate\b", re.IGNORECASE),
    re.compile(r"\bproduct\s+intern\b", re.IGNORECASE),
    re.compile(r"\bapm\b", re.IGNORECASE),
    re.compile(r"\bproduct\s+manager\b", re.IGNORECASE),
]

# ADJACENT_PRODUCT — role titles that are product-adjacent per spec
_ADJACENT_PRODUCT_PATTERNS = [
    re.compile(r"\bproduct\s+anal(yst|ysis)\b", re.IGNORECASE),
    re.compile(r"\bproduct\s+op(eration)?s\b", re.IGNORECASE),
    re.compile(r"\bgrowth\b", re.IGNORECASE),
    re.compile(r"\bfounders?\s+office\b", re.IGNORECASE),
    re.compile(r"\bfounding\s+team\b", re.IGNORECASE),
    re.compile(r"\bchief\s+of\s+staff\b", re.IGNORECASE),
    re.compile(r"\bproduct\s+strategy\b", re.IGNORECASE),
]

# Product-responsibility signals used to validate ADJACENT roles from description
_PRODUCT_RESP_SIGNALS = [
    "product roadmap", "product requirements", "user stories", "prd",
    "feature prioritization", "a/b test", "product metrics", "go-to-market",
    "sprint", "backlog", "product discovery", "customer discovery",
    "product thinking", "product development",
]

# Unpaid / incompatible employment type
_EMPLOYMENT_REJECT_PATTERNS = [
    re.compile(r"\bunpaid\b", re.IGNORECASE),
    re.compile(r"\bfreelance[\s-]only\b", re.IGNORECASE),
    re.compile(r"\bcontract[\s-]only\b", re.IGNORECASE),
    re.compile(r"\bpart[\s-]time[\s-]only\b", re.IGNORECASE),
    re.compile(r"\bno\s+compensation\b", re.IGNORECASE),
    re.compile(r"\bunpaid\s+intern", re.IGNORECASE),
]

# Elite-college-only signals → REVIEW (not a hard reject)
_EDUCATION_REVIEW_PATTERNS = [
    re.compile(r"\b(iit|iim)[\s-]only\b", re.IGNORECASE),
    re.compile(r"\bfrom\s+(iit|iim)\b", re.IGNORECASE),
    re.compile(r"\bpremier\s+(institute|college)[\s-]only\b", re.IGNORECASE),
    re.compile(r"\btier[\s-]?1\s+(college|institute)[\s-]only\b", re.IGNORECASE),
]

# Explicit foreign-country work authorization requirements
_WORK_AUTH_REJECT_PATTERNS = [
    re.compile(
        r"\bauthorized\s+to\s+work\s+in\s+(the\s+)?(us|usa|uk|canada|australia|germany|europe)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bus\s+work\s+authorization\b", re.IGNORECASE),
    re.compile(
        r"\b(must|require[sd]?)\s+.{0,30}(visa|work\s+permit)\s+(in|for)\s+(the\s+)?(us|usa|uk|canada)\b",
        re.IGNORECASE,
    ),
]

# Range max extractor: group(1) is the upper bound from "M-N years"
_RANGE_MAX_RE = re.compile(r"\d+\s*[-–]\s*(\d+)\s*\+?\s*(?:yrs?|years?)", re.IGNORECASE)


def _parse_max_years(text):
    m = _RANGE_MAX_RE.search(text or "")
    return int(m.group(1)) if m else None


def _title_from(job):
    text = job.get("text", "") or ""
    return text.split("|")[0].strip()


def _location_from(job):
    text = job.get("text", "") or ""
    parts = text.split("|")
    return parts[1].strip() if len(parts) >= 2 else ""


# ---------------------------------------------------------------------------
# Individual criterion checks — each returns (status, reason)
# ---------------------------------------------------------------------------

def _seniority(title):
    # Whitelist: known eligible titles that contain a seniority-looking keyword
    for pat in _SENIORITY_WHITELIST_PATTERNS:
        if pat.search(title):
            return "ELIGIBLE", "Whitelisted entry-level title"

    for pat in _SENIORITY_REJECT_PATTERNS:
        m = pat.search(title)
        if m:
            return "REJECT", f"Seniority marker in title: '{m.group(0)}'"
    return "ELIGIBLE", "No seniority marker"


def _experience(experience_range, description):
    exp_text = experience_range or ""
    desc = description or ""

    if has_fresher_signal(exp_text) or has_fresher_signal(desc):
        return "ELIGIBLE", "Fresher/entry-level signal"

    min_y = parse_min_experience(exp_text)
    if min_y is None:
        min_y = parse_min_experience(desc)

    if min_y is None or min_y == 0:
        return "ELIGIBLE", "No experience requirement"

    if min_y >= HARD_CUTOFF_MIN_YEARS:
        return "REJECT", f"Requires {min_y}+ years (exceeds {HARD_CUTOFF_MIN_YEARS}-year cutoff)"

    # min_y is 1 or 2 — check the upper bound
    max_y = _parse_max_years(exp_text) or _parse_max_years(desc)

    if max_y is None:
        # Single-value mention: "1 year" → ELIGIBLE; "2 years" → ambiguous
        if min_y <= 1:
            return "ELIGIBLE", f"Requires {min_y} year(s)"
        return "REVIEW", "Requires 2 years (ambiguous range)"

    if max_y <= 2:
        return "ELIGIBLE", f"Requires {min_y}–{max_y} years"
    if max_y == 3:
        return "REVIEW", f"Requires {min_y}–{max_y} years (upper bound reaches 3)"
    # max_y > 3 — wide range e.g. "1-5 years"; keep as REVIEW, not hard reject
    return "REVIEW", f"Requires {min_y}–{max_y} years (wide range)"


def _role_category(title, description):
    """Returns (role_category, status, reason)."""
    for pat in _PRIMARY_PRODUCT_PATTERNS:
        if pat.search(title):
            return "PRIMARY_PRODUCT", "ELIGIBLE", "Title matches primary product role"

    for pat in _ADJACENT_PRODUCT_PATTERNS:
        if pat.search(title):
            if description:
                desc_lower = description.lower()
                if any(sig in desc_lower for sig in _PRODUCT_RESP_SIGNALS):
                    return "ADJACENT_PRODUCT", "ELIGIBLE", "Adjacent role with product responsibilities confirmed"
            return "ADJACENT_PRODUCT", "ELIGIBLE", "Title matches adjacent product role"

    # Fallback: title contains "product" but no specific pattern matched
    if "product" in title.lower():
        return "PRIMARY_PRODUCT", "ELIGIBLE", "Title contains 'product'"

    # Check description for product signals before declaring NOT_PRODUCT
    if description:
        desc_lower = description.lower()
        hits = sum(1 for sig in _PRODUCT_RESP_SIGNALS if sig in desc_lower)
        if hits >= 2:
            return "ADJACENT_PRODUCT", "ELIGIBLE", "Description contains product responsibility signals"

    return "NOT_PRODUCT", "REJECT", "Role does not appear to be product-oriented"


def _location(job):
    combined = " ".join([
        _location_from(job),
        (job.get("text", "") or ""),
        (job.get("description", "") or ""),
    ]).lower()

    for pattern in config.LOCATION_REJECT_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            return "REJECT", "International-only location restriction detected"

    for kw in config.LOCATION_ALLOW_KEYWORDS:
        if kw in combined:
            return "ELIGIBLE", "India/remote location confirmed"

    # Scrapers already gate on location; ambiguous → give benefit of the doubt
    return "ELIGIBLE", "Location not restricted to outside India"


def _employment(description):
    if not description:
        return "ELIGIBLE", "No employment type restriction"
    for pat in _EMPLOYMENT_REJECT_PATTERNS:
        if pat.search(description):
            return "REJECT", f"Incompatible employment type detected"
    return "ELIGIBLE", "Employment type compatible"


def _education(description):
    if not description:
        return "ELIGIBLE", "No education restriction"
    for pat in _EDUCATION_REVIEW_PATTERNS:
        if pat.search(description):
            return "REVIEW", "Elite-college-only requirement detected"
    return "ELIGIBLE", "No restrictive education requirement"


def _work_authorization(description):
    if not description:
        return "ELIGIBLE", "No work authorization restriction"
    for pat in _WORK_AUTH_REJECT_PATTERNS:
        if pat.search(description):
            return "REJECT", "Incompatible work authorization requirement detected"
    return "ELIGIBLE", "No incompatible work authorization requirement"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_eligibility(job):
    """
    Evaluate one job dict against all eligibility criteria.

    Input: a job dict from the DB (keys: text, experience_range, description, …)

    Output dict keys:
        eligibility_status          : ELIGIBLE | REVIEW | REJECT
        eligibility_reason          : human-readable explanation
        role_category               : PRIMARY_PRODUCT | ADJACENT_PRODUCT | NOT_PRODUCT
        experience_status           : ELIGIBLE | REVIEW | REJECT
        location_status             : ELIGIBLE | REJECT
        employment_status           : ELIGIBLE | REJECT
        seniority_status            : ELIGIBLE | REJECT
        freshness_status            : ELIGIBLE (scrapers gate at insert time)
        education_status            : ELIGIBLE | REVIEW
        work_authorization_status   : ELIGIBLE | REJECT
    """
    title = _title_from(job)
    description = job.get("description", "") or ""
    experience_range = job.get("experience_range", "") or ""

    seniority_st, seniority_reason = _seniority(title)
    experience_st, experience_reason = _experience(experience_range, description)
    role_cat, role_st, role_reason = _role_category(title, description)
    location_st, location_reason = _location(job)
    employment_st, employment_reason = _employment(description)
    education_st, education_reason = _education(description)
    work_auth_st, work_auth_reason = _work_authorization(description)
    freshness_st = "ELIGIBLE"

    # Collect hard rejects first
    reject_parts = []
    if seniority_st == "REJECT":
        reject_parts.append(seniority_reason)
    if experience_st == "REJECT":
        reject_parts.append(experience_reason)
    if role_st == "REJECT":
        reject_parts.append(role_reason)
    if location_st == "REJECT":
        reject_parts.append(location_reason)
    if employment_st == "REJECT":
        reject_parts.append(employment_reason)
    if work_auth_st == "REJECT":
        reject_parts.append(work_auth_reason)

    if reject_parts:
        overall_status = "REJECT"
        overall_reason = "; ".join(reject_parts)
    else:
        # Collect review flags
        review_parts = []
        if experience_st == "REVIEW":
            review_parts.append(experience_reason)
        if education_st == "REVIEW":
            review_parts.append(education_reason)

        if review_parts:
            overall_status = "REVIEW"
            overall_reason = "; ".join(review_parts)
        else:
            overall_status = "ELIGIBLE"
            overall_reason = "All criteria passed"

    return {
        "eligibility_status": overall_status,
        "eligibility_reason": overall_reason,
        "role_category": role_cat,
        "experience_status": experience_st,
        "location_status": location_st,
        "employment_status": employment_st,
        "seniority_status": seniority_st,
        "freshness_status": freshness_st,
        "education_status": education_st,
        "work_authorization_status": work_auth_st,
    }
