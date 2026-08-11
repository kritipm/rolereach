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
    headers = {
        "X-API-KEY": config.SERPER_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "q": query,
        "location": "India",
        "num": 10,
    }
    resp = requests.post(
        config.SERPER_JOBS_URL,
        headers=headers,
        json=payload,
        timeout=config.REQUEST_TIMEOUT_SECONDS,
    )
    print(f"[Serper] Query '{query}' status: {resp.status_code}")
    if resp.status_code != 200:
        print(f"[Serper] Error response: {resp.text[:300]}")
    resp.raise_for_status()
    results = resp.json().get("jobs", [])
    if not results:
        print(f"[Serper] 0 jobs returned. Response: {str(resp.json())[:200]}")
    return results


def is_excluded_title(title):
    lowered = title.lower()
    return any(keyword in lowered for keyword in config.TITLE_EXCLUDE_KEYWORDS)


def is_location_allowed(location):
    lowered = (location or "").lower()
    return any(keyword in lowered for keyword in config.GOOGLE_JOBS_LOCATION_ALLOW_KEYWORDS)


def is_excluded_company(company_name):
    lowered = (company_name or "").lower()
    return any(keyword in lowered for keyword in config.GOOGLE_JOBS_COMPANY_EXCLUDE_KEYWORDS)


def is_recently_posted(posted_at_str):
    """True if posted within the last 48 hours. Unknown date = include."""
    if not posted_at_str:
        return True
    lowered = posted_at_str.lower().strip()
    if any(x in lowered for x in ("hour", "today", "just now", "minute")):
        return True
    match = re.search(r'(\d+)\s+day', lowered)
    if match:
        return int(match.group(1)) <= 7
    if any(x in lowered for x in ("week", "month", "30+")):
        return False
    return True


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
    if not company_name or company_name == "unknown":
        return None

    headers = {
        "X-API-KEY": config.SERPER_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {"q": f"{company_name} official website"}
    try:
        resp = requests.post(
            config.SERPER_SEARCH_URL,
            headers=headers,
            json=payload,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None

    for result in resp.json().get("organic", []):
        link = result.get("link") or ""
        domain = urllib.parse.urlparse(link).netloc.lower()
        if is_non_official_domain(domain):
            continue
        return link

    return None


def build_job(job, query, experience_text):
    title = job.get("title") or job.get("job_title") or ""
    company_name = job.get("companyName") or job.get("company_name") or "unknown"
    location = job.get("location") or ""
    via = job.get("source") or job.get("via") or ""
    job_id = job.get("jobId") or job.get("job_id") or job.get("source_link") or title
    posted_at = (
        job.get("date")
        or (job.get("detected_extensions") or {}).get("posted_at", "")
        or ""
    )

    return {
        "comment_id": job_id_to_int(job_id),
        "thread_id": 0,
        "author": company_name,
        "posted_at": posted_at,
        "matched_keyword": query,
        "text": f"{title} | {location} | via {via}",
        "url": job.get("applyLink") or job.get("source_link") or job.get("share_link") or "",
        "company_url": find_company_website(company_name),
        "verified": False,
        "source": "google_jobs",
        "external_id": str(job_id),
        "experience_range": experience_filter.extract_experience_range(experience_text),
    }


def scrape_google_jobs_pm_jobs():
    matches = []
    with database.get_connection() as conn:
        for query in config.GOOGLE_JOBS_QUERIES:
            try:
                jobs = fetch_jobs_for_query(query)
            except requests.RequestException as e:
                print(f"[Serper] Query '{query}' failed: {e}")
                continue

            for job in jobs:
                title = job.get("title") or job.get("job_title") or ""
                if not title:
                    continue
                if is_excluded_title(title):
                    continue
                company_name = job.get("companyName") or job.get("company_name") or ""
                if is_excluded_company(company_name):
                    continue
                location = job.get("location") or ""
                if not is_location_allowed(location):
                    continue
                if not experience_filter.has_product_in_title(title):
                    continue
                experience_text = " ".join(job.get("highlights", {}).get("items", []) if isinstance(job.get("highlights"), dict) else []) + " " + (job.get("description") or job.get("snippet") or "")
                posted_at_str = job.get("date") or (job.get("detected_extensions") or {}).get("posted_at", "")
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
