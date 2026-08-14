import hashlib
import re
from datetime import datetime, timezone, timedelta

import requests

import config
import database
import fix_company_names
from scraper_careers import matches_title

# Instahyre's real public job-search API (verified live via browser network capture —
# the literal /api/v1/opportunity/?role=...&experience=... endpoint from the original
# spec 404s; it doesn't exist). This is the same endpoint their own search-jobs page
# calls, found by watching its network requests directly.
INSTAHYRE_SEARCH_URL = "https://www.instahyre.com/api/v1/job_search"
INSTAHYRE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# The list API returns no posting date at all (no created_at field, on this or any
# other field on the object). The only place a date exists is the individual job's
# own HTML page, as Google-for-Jobs JSON-LD: "datePosted": "YYYY-MM-DD".
DATE_POSTED_PATTERN = re.compile(r'"datePosted"\s*:\s*"(\d{4}-\d{2}-\d{2})"')


def is_recently_posted(posted_date, days=7):
    """True if posted_date is within the last `days` days. Unknown date = include."""
    if posted_date is None:
        return True
    if posted_date.tzinfo is None:
        posted_date = posted_date.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - posted_date) <= timedelta(days=days)


def fetch_posted_date(public_url):
    """Visit the job's own detail page and pull its Google-for-Jobs `datePosted`
    (the list API has no date field of any kind — see DATE_POSTED_PATTERN above).
    Only called for jobs that already passed the title filter, same as
    scraper_cutshort.py's per-job fetch_job_description()."""
    if not public_url:
        return None

    try:
        resp = requests.get(public_url, headers=INSTAHYRE_HEADERS, timeout=config.REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    match = DATE_POSTED_PATTERN.search(resp.text)
    if not match:
        return None

    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def to_comment_id(external_id):
    """Hash Instahyre's job id into a positive 63-bit int for the shared PK."""
    raw = f"instahyre:{external_id}"
    return int.from_bytes(hashlib.sha256(raw.encode()).digest()[:8], "big") & 0x7FFFFFFFFFFFFFFF


# Instahyre's results skew more senior/generic than the curated ATS lists in
# scraper_careers.py, so this is a second, stricter pass on top of matches_title()'s
# shared TITLE_EXCLUDE_KEYWORDS — deliberately kept local to this file rather than
# added to scraper_careers.py, so it doesn't change filtering for the other scrapers.
SENIORITY_EXCLUDE_KEYWORDS = [
    "senior",
    "sr.",
    "lead",
    "head",
    "director",
    "vp",
    "principal",
]

# Rough experience-in-title/experience_range text filter. Checked against
# "X+ years" phrasing specifically (not bare "5+"), since Instahyre titles that
# state a number at all almost always spell out "years" right after it.
EXPERIENCE_EXCLUDE_PATTERNS = [
    "5+ years",
    "6+ years",
    "7+ years",
    "8+ years",
    "10+ years",
]

EXPERIENCE_RANGE_LABEL = "0-2 years"


def fails_seniority_filter(title):
    lowered = title.lower()
    return any(keyword in lowered for keyword in SENIORITY_EXCLUDE_KEYWORDS)


def fails_experience_text_filter(title, experience_range):
    haystack = f"{title} {experience_range or ''}".lower()
    return any(pattern in haystack for pattern in EXPERIENCE_EXCLUDE_PATTERNS)


def build_job(job, posted_date):
    job_id = str(job["id"])
    title = job.get("title") or ""
    company = (job.get("employer") or {}).get("company_name") or "unknown"
    location = job.get("locations") or "India"
    url = job.get("public_url") or f"https://www.instahyre.com/job-{job_id}/"

    return {
        "comment_id": to_comment_id(job_id),
        "thread_id": 0,
        "author": company,
        "posted_at": posted_date.isoformat() if posted_date else "",
        "matched_keyword": title,
        "text": f"{title} | {location} | via Instahyre",
        "url": url,
        "company_url": None,
        "verified": False,
        "source": "instahyre",
        "external_id": job_id,
        "experience_range": EXPERIENCE_RANGE_LABEL,
        "description": None,
    }


# Targeted skill-tag queries, replacing the broad "Product Manager" search. Each is
# Instahyre's own skill-tag filter, confirmed live — note "APM" alone is a loose
# match (also returns unrelated titles like "Solution Architect", presumably via
# other skill tags containing "APM"), but matches_title() downstream still filters
# those out, so it's harmless to include as-is.
SKILL_QUERIES = [
    "Associate Product Manager",
    "APM",
    "Product Analyst",
]


def fetch_instahyre_jobs(skill_query):
    # 35 is a hard server-side page cap (a larger `limit` is silently ignored) so
    # this is a single-page fetch per query, no pagination.
    params = {
        "company_size": 0,
        "job_type": 0,
        "offset": 0,
        "source": "opportunities",
        "skills": skill_query,
    }
    try:
        resp = requests.get(
            INSTAHYRE_SEARCH_URL,
            params=params,
            headers=INSTAHYRE_HEADERS,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        print(f"[Instahyre] query {skill_query!r} request failed: {e}")
        return []

    if resp.status_code != 200:
        print(f"[Instahyre] query {skill_query!r} non-200 status: {resp.status_code}")
        return []

    try:
        data = resp.json()
    except ValueError:
        print(f"[Instahyre] query {skill_query!r} invalid JSON response")
        return []

    return data.get("objects", [])


def scrape_instahyre_pm_jobs():
    matches = []
    total_fetched = 0
    seen_job_ids = set()
    deduped_jobs = []

    for skill_query in SKILL_QUERIES:
        jobs = fetch_instahyre_jobs(skill_query)
        total_fetched += len(jobs)
        for job in jobs:
            job_id = job.get("id")
            if job_id in seen_job_ids:
                continue
            seen_job_ids.add(job_id)
            deduped_jobs.append(job)

    with database.get_connection() as conn:
        for job in deduped_jobs:
            title = job.get("title") or ""
            if not matches_title(title):
                continue

            if fails_seniority_filter(title):
                continue

            if fails_experience_text_filter(title, EXPERIENCE_RANGE_LABEL):
                continue

            posted_date = fetch_posted_date(job.get("public_url"))
            if not is_recently_posted(posted_date):
                continue

            record = build_job(job, posted_date)
            database.save_job(conn, record)
            matches.append(record)

        conn.commit()

    print(f"[Instahyre] {total_fetched} total | {len(matches)} matched")
    return matches


if __name__ == "__main__":
    database.init_db()
    results = scrape_instahyre_pm_jobs()
    print(f"\nInstahyre PM postings found: {len(results)}\n")
    for job in results:
        print(f"- [{job['author']}] {job['url']}")
        print(f"  {job['matched_keyword']}\n")

    changes, unresolved = fix_company_names.fix_names()
    if changes:
        print(f"fix_company_names: corrected {len(changes)} name(s):")
        for old_name, new_name in changes:
            print(f"  {old_name!r} -> {new_name!r}")
