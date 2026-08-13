import requests
import config
import database
import experience_filter
import fix_company_names
import sys

sys.stdout.reconfigure(encoding="utf-8")

JSEARCH_URL = "https://jsearch.p.rapidapi.com/search-v2"
JSEARCH_HEADERS = {
    "X-RapidAPI-Key": config.JSEARCH_API_KEY,
    "X-RapidAPI-Host": "jsearch.p.rapidapi.com"
}

QUERIES = [
    "product manager 0 to 2 years experience India",
    "associate product manager India",
    "APM India startup",
    "product analyst India",
    "growth product manager India",
    "founders office product India",
    "junior product manager India remote",
    "product manager fresher India",
]

def fetch_jobs_for_query(query):
    params = {
        "query": query,
        "page": "1",
        "num_pages": "1",
        "country": "in",
        "date_posted": "week",
    }
    resp = requests.get(
        JSEARCH_URL,
        headers=JSEARCH_HEADERS,
        params=params,
        timeout=30,
    )
    print(f"[JSearch] Query '{query}' status: {resp.status_code}")
    if resp.status_code != 200:
        print(f"[JSearch] Error: {resp.text[:200]}")
        resp.raise_for_status()

    data = resp.json()
    jobs_data = data.get("data", [])

    print(f"[JSearch] data type: {type(jobs_data).__name__}, length/keys: {len(jobs_data) if isinstance(jobs_data, list) else list(jobs_data.keys())[:5] if isinstance(jobs_data, dict) else 'unknown'}")

    import json
    jobs_list = jobs_data.get("jobs", []) if isinstance(jobs_data, dict) else (jobs_data if isinstance(jobs_data, list) else [])
    print(f"[JSearch DEBUG] Jobs in response: {len(jobs_list)}")
    if jobs_list:
        print(f"[JSearch DEBUG] First job keys: {list(jobs_list[0].keys())[:10]}")
        print(f"[JSearch DEBUG] First job sample: {json.dumps(jobs_list[0], indent=2)[:500]}")
    else:
        print(f"[JSearch DEBUG] Empty jobs list. Full data keys: {list(jobs_data.keys()) if isinstance(jobs_data, dict) else 'data is a ' + type(jobs_data).__name__}")
        print(f"[JSearch DEBUG] Cursor: {jobs_data.get('cursor') if isinstance(jobs_data, dict) else None}")

    if isinstance(jobs_data, list):
        return [j for j in jobs_data if isinstance(j, dict)]
    elif isinstance(jobs_data, dict):
        # data might be nested — check for jobs inside
        print(f"[JSearch] data dict keys: {list(jobs_data.keys())}")
        for key in ["jobs", "results", "items", "job_listings"]:
            if key in jobs_data:
                items = jobs_data[key]
                if isinstance(items, list):
                    return [j for j in items if isinstance(j, dict)]
        return []
    return []

def is_excluded_title(title):
    lowered = title.lower()
    return any(keyword in lowered for keyword in config.TITLE_EXCLUDE_KEYWORDS)

def is_location_allowed(location):
    lowered = (location or "").lower()
    return any(keyword in lowered for keyword in config.GOOGLE_JOBS_LOCATION_ALLOW_KEYWORDS)

def build_job(job, query):
    import hashlib
    title = job.get("job_title") or ""
    company = job.get("employer_name") or "unknown"
    location = f"{job.get('job_city', '')} {job.get('job_country', '')}".strip()
    job_id = job.get("job_id") or title + company
    id_int = int.from_bytes(hashlib.sha256(job_id.encode()).digest()[:8], "big") & 0x7FFFFFFFFFFFFFFF
    experience_text = job.get("job_description") or ""

    return {
        "comment_id": id_int,
        "thread_id": 0,
        "author": company,
        "posted_at": job.get("job_posted_at_datetime_utc") or "",
        "matched_keyword": query,
        "text": f"{title} | {location} | via JSearch",
        "url": job.get("job_apply_link") or job.get("job_google_link") or "",
        "company_url": job.get("employer_website") or "",
        "verified": False,
        "source": "jsearch",
        "external_id": job_id,
        "experience_range": experience_filter.extract_experience_range(experience_text),
        "description": experience_text[:500] if experience_text else None,
    }

def scrape_jsearch_pm_jobs():
    matches = []
    with database.get_connection() as conn:
        known_ids = {
            row["comment_id"]
            for row in conn.execute(
                "SELECT comment_id FROM jobs WHERE source = 'jsearch'"
            ).fetchall()
        }

        for query in QUERIES:
            try:
                jobs = fetch_jobs_for_query(query)
            except requests.RequestException as e:
                print(f"[JSearch] Query '{query}' failed: {e}")
                continue

            for job in jobs:
                title = job.get("job_title") or ""
                if not title:
                    continue
                if is_excluded_title(title):
                    continue
                if not experience_filter.has_product_in_title(title):
                    continue

                location = f"{job.get('job_city', '')} {job.get('job_state', '')} {job.get('job_country', '')}".strip()
                if not is_location_allowed(location):
                    continue

                experience_text = job.get("job_description") or ""
                min_years = experience_filter.parse_min_experience(experience_text)
                if not experience_filter.is_experience_allowed(min_years, experience_text):
                    continue

                record = build_job(job, query)
                if record["comment_id"] in known_ids:
                    continue

                database.save_job(conn, record)
                known_ids.add(record["comment_id"])
                matches.append(record)

        conn.commit()
    return matches

if __name__ == "__main__":
    database.init_db()
    results = scrape_jsearch_pm_jobs()
    print(f"\nJSearch PM postings found: {len(results)}\n")
    for job in results:
        print(f"- [{job['author']}] {job['url']}")
        print(f"  {job['matched_keyword']}\n")

    changes, unresolved = fix_company_names.fix_names()
    if changes:
        print(f"fix_company_names: corrected {len(changes)} name(s):")
        for old_name, new_name in changes:
            print(f"  {old_name!r} -> {new_name!r}")
