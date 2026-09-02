import hashlib
import re
import sys
import urllib.parse

import requests

import config
import database
import experience_filter
import fix_company_names

sys.stdout.reconfigure(encoding="utf-8")


def fetch_jobs_for_query(query):
    for key in [config.SERPAPI_KEY, config.SERPAPI_KEY_2]:
        if not key:
            continue
        params = {
            "engine": "google_jobs",
            "q": query,
            "api_key": key,
            "hl": "en",
        }
        resp = requests.get(
            config.SERPAPI_URL,
            params=params,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code == 429 or (resp.status_code == 200 and resp.json().get("error")):
            print(f"[GoogleJobs] Key exhausted, trying next key")
            continue
        resp.raise_for_status()
        return resp.json().get("jobs_results", [])
    print("[GoogleJobs] All SerpAPI keys exhausted")
    return []


def is_excluded_title(title):
    lowered = title.lower()
    return any(keyword in lowered for keyword in config.TITLE_EXCLUDE_KEYWORDS)


def is_location_allowed(location):
    lowered = (location or "").lower()
    return any(keyword in lowered for keyword in config.GOOGLE_JOBS_LOCATION_ALLOW_KEYWORDS)


def is_excluded_company(company_name):
    lowered = (company_name or "").lower()
    return any(keyword in lowered for keyword in config.GOOGLE_JOBS_COMPANY_EXCLUDE_KEYWORDS)


# Literal substring check, deliberately separate from experience_filter.py's
# regex-based numeric parsing — catches phrasing ("minimum 3", "at least 5")
# that pattern wouldn't necessarily recognize as a number+years expression.
SENIOR_EXPERIENCE_PATTERNS = [
    "3-5 years", "4-6 years", "5-7 years", "5-8 years", "6-8 years", "6-9 years",
    "7-9 years", "7-10 years", "8-10 years", "3+ years", "4+ years", "5+ years",
    "6+ years", "7+ years", "8+ years", "minimum 3", "minimum 4", "minimum 5",
    "at least 3", "at least 4", "at least 5",
]


def has_senior_experience_requirement(title, description):
    haystack = f"{title} {description or ''}".lower()
    return any(pattern in haystack for pattern in SENIOR_EXPERIENCE_PATTERNS)


def is_recently_posted(posted_at_str):
    if not posted_at_str:
        return True
    lowered = posted_at_str.lower().strip()
    if any(x in lowered for x in ("hour", "today", "just now", "minute")):
        return True
    if "month" in lowered or "week" in lowered:
        return False
    match = re.search(r'(\d+)\s+day', lowered)
    if match:
        return int(match.group(1)) <= 14
    return False


def job_id_to_int(job_id):
    digest = hashlib.sha256(job_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


def is_non_official_domain(domain):
    """Domain-boundary match (not substring) — 'x.com' must not match 'mudrex.com'."""
    normalized = domain[4:] if domain.startswith("www.") else domain

    for bad in config.NON_OFFICIAL_WEBSITE_DOMAINS:
        if bad.endswith("."):
            prefix = bad[:-1]
            if normalized == prefix or normalized.startswith(prefix + "."):
                return True
        elif normalized == bad or normalized.endswith("." + bad):
            return True

    return False


def find_company_website(company_name):
    """Search Google (via SerpApi) for the company's actual official site,
    skipping job boards, aggregators, and social/media platforms."""
    if not company_name or company_name == "unknown":
        return None

    params = {
        "engine": "google",
        "q": f"{company_name} official website",
        "api_key": config.SERPAPI_KEY,
        "hl": "en",
    }
    try:
        resp = requests.get(config.SERPAPI_URL, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    for result in resp.json().get("organic_results", []):
        link = result.get("link") or ""
        domain = urllib.parse.urlparse(link).netloc.lower()
        if is_non_official_domain(domain):
            continue
        return link

    return None


def build_job(job, query, experience_text):
    title = job.get("title") or job.get("job_title") or ""
    company_name = job.get("company_name", "unknown")
    job_id = job.get("job_id") or job.get("source_link") or title

    return {
        "comment_id": job_id_to_int(job_id),
        "thread_id": 0,
        "author": company_name,
        "posted_at": (job.get("detected_extensions") or {}).get("posted_at", ""),
        "matched_keyword": query,
        "text": f"{title} | {job.get('location', '')} | via {job.get('via', '')}",
        "url": job.get("source_link") or job.get("share_link") or "",
        "company_url": find_company_website(company_name),
        "verified": False,
        "source": "google_jobs",
        "external_id": job_id,
        "experience_range": experience_filter.extract_experience_range(experience_text),
    }


def scrape_google_jobs_pm_jobs():
    matches = []
    with database.get_connection() as conn:
        for query in config.GOOGLE_JOBS_QUERIES:
            try:
                jobs = fetch_jobs_for_query(query)
            except requests.RequestException:
                continue

            for job in jobs:
                title = job.get("title") or job.get("job_title") or ""
                if not title:
                    continue
                if is_excluded_title(title):
                    continue
                if is_excluded_company(job.get("company_name")):
                    continue
                if not is_location_allowed(job.get("location")):
                    continue
                if not experience_filter.has_product_in_title(title):
                    continue
                if has_senior_experience_requirement(title, job.get("description")):
                    print(f"[GoogleJobs] Skipped senior experience requirement: {title}")
                    continue
                experience_text = " ".join(job.get("extensions") or []) + " " + (job.get("description") or "")
                posted_at_str = (job.get("detected_extensions") or {}).get("posted_at", "")
                if not is_recently_posted(posted_at_str):
                    continue
                min_years = experience_filter.parse_min_experience(experience_text)
                if not experience_filter.is_experience_allowed(min_years, f"{title} {experience_text}"):
                    continue

                record = build_job(job, query, experience_text)
                database.save_job(conn, record)
                matches.append(record)

        conn.commit()

    return matches


if __name__ == "__main__":
    database.init_db()
    results = scrape_google_jobs_pm_jobs()
    print(f"Google Jobs PM postings found: {len(results)}\n")
    for job in results:
        print(f"- [{job['author']}] {job['url']}")
        print(f"  {job['text']}")
        print(f"  matched query: {job['matched_keyword']}\n")

    changes, unresolved = fix_company_names.fix_names()
    if changes:
        print(f"fix_company_names: corrected {len(changes)} name(s):")
        for old_name, new_name in changes:
            print(f"  {old_name!r} -> {new_name!r}")
