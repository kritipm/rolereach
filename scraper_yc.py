import requests
import sys
import hashlib
from bs4 import BeautifulSoup
import database
import experience_filter
import config
import fix_company_names

sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "https://www.ycombinator.com"
JOBS_URL = "https://www.ycombinator.com/jobs"

ROLE_KEYWORDS = [
    "product manager",
    "product management",
    "associate product",
    "apm",
    "growth",
    "founders office",
    "founding team",
    "chief of staff",
    "product analyst",
    "growth associate",
    "product growth",
]

def url_to_id(url):
    digest = hashlib.sha256(url.encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF

def fetch_jobs_html():
    import urllib.parse
    api_url = (
        f"{config.SCRAPERAPI_BASE_URL}?api_key={config.SCRAPERAPI_KEY}"
        f"&url={urllib.parse.quote(JOBS_URL, safe='')}&render=true"
    )
    resp = requests.get(api_url, timeout=90)
    resp.raise_for_status()
    return resp.text

def matches_role(text):
    lowered = text.lower()
    return any(kw in lowered for kw in ROLE_KEYWORDS)

def is_india_or_remote(location):
    lowered = (location or "").lower()
    return any(kw in lowered for kw in ["india", "remote", "anywhere", "worldwide"])

def parse_jobs(html):
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen_urls = set()

    for card in soup.find_all("a", href=True):
        href = card.get("href", "")

        # Only actual job postings — not category pages
        if not href.startswith("/jobs/") or any(x in href for x in [
            "/role/", "/san-francisco", "/new-york", "/los-angeles",
            "/remote", "/london", "/berlin", "/singapore"
        ]):
            continue

        job_url = BASE_URL + href
        if job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        title = card.get_text(strip=True)[:120]
        if not title or not matches_role(title):
            continue

        # Try to find company name from parent elements
        company = ""
        parent = card.find_parent()
        if parent:
            company_el = parent.find_previous_sibling()
            if company_el:
                company = company_el.get_text(strip=True)[:60]

        # Location
        location = "Remote"
        location_el = card.find_next_sibling()
        if location_el:
            loc_text = location_el.get_text(strip=True)
            if any(kw in loc_text.lower() for kw in ["remote", "india", "anywhere"]):
                location = loc_text[:60]

        jobs.append({
            "title": title,
            "url": job_url,
            "company": company,
            "location": location,
        })

    print(f"[YC] Found {len(jobs)} matching job links after filtering")
    return jobs

def scrape_yc_jobs():
    try:
        html = fetch_jobs_html()
    except requests.RequestException as e:
        print(f"[YC] Fetch failed: {e}")
        return []

    jobs = parse_jobs(html)
    matches = []

    with database.get_connection() as conn:
        known_ids = {
            row["comment_id"]
            for row in conn.execute(
                "SELECT comment_id FROM jobs WHERE source = 'yc'"
            ).fetchall()
        }

        for job in jobs:
            job_id = url_to_id(job["url"])
            if job_id in known_ids:
                continue

            record = {
                "comment_id": job_id,
                "thread_id": 0,
                "author": job["company"] or "YC Company",
                "posted_at": "",
                "matched_keyword": job["title"],
                "text": f"{job['title']} | {job['location']} | YC",
                "url": job["url"],
                "company_url": "",
                "verified": True,
                "source": "yc",
                "external_id": str(job_id),
                "experience_range": "Not specified",
                "description": None,
            }

            database.save_job(conn, record)
            known_ids.add(job_id)
            matches.append(record)

        conn.commit()

    return matches

if __name__ == "__main__":
    database.init_db()
    results = scrape_yc_jobs()
    print(f"\nYC Jobs postings found: {len(results)}\n")
    for job in results:
        print(f"- [{job['author']}] {job['url']}")
        print(f"  {job['matched_keyword']}\n")

    changes, unresolved = fix_company_names.fix_names()
    if changes:
        print(f"fix_company_names: corrected {len(changes)} name(s):")
        for old_name, new_name in changes:
            print(f"  {old_name!r} -> {new_name!r}")
