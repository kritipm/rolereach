import re
import sys
import hashlib
import urllib.parse
import requests
from bs4 import BeautifulSoup
import config
import database
import experience_filter
import fix_company_names

sys.stdout.reconfigure(encoding="utf-8")

ROLE_KEYWORDS = [
    "product manager", "product management", "associate product",
    "apm", "growth", "founders office", "founding team",
    "product analyst", "growth associate", "chief of staff",
    "product growth", "product lead", "pm ",
]

EXCLUDE_KEYWORDS = [
    "senior product manager", "lead product manager", "director of product",
    "vp of product", "head of product", "principal product",
    "sr. product manager", "sr product manager",
]

COMPANIES = [
    {"name": "Razorpay", "url": "https://razorpay.com/jobs/"},
    {"name": "Groww", "url": "https://groww.in/careers"},
    {"name": "CRED", "url": "https://careers.cred.club/"},
    {"name": "Meesho", "url": "https://meesho.io/jobs"},
    {"name": "Zepto", "url": "https://www.zeptonow.com/careers"},
    {"name": "PhonePe", "url": "https://www.phonepe.com/careers/"},
    {"name": "Zomato", "url": "https://www.zomato.com/careers"},
    {"name": "Swiggy", "url": "https://careers.swiggy.com/"},
    {"name": "Slice", "url": "https://www.sliceit.com/careers"},
    {"name": "Jupiter", "url": "https://jupiter.money/careers/"},
    {"name": "Nykaa", "url": "https://careers.nykaa.com/"},
    {"name": "BharatPe", "url": "https://bharatpe.com/careers"},
    {"name": "Freshworks", "url": "https://careers.freshworks.com/"},
    {"name": "Darwinbox", "url": "https://darwinbox.com/careers"},
    {"name": "Urban Company", "url": "https://www.urbancompany.com/careers"},
]

def url_to_id(url, company):
    key = f"{company}::{url}"
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF

def fetch_page(url):
    for key in [config.SCRAPERAPI_KEY_3, config.SCRAPERAPI_KEY, config.SCRAPERAPI_KEY_2]:
        if not key:
            continue
        api_url = (
            f"{config.SCRAPERAPI_BASE_URL}?api_key={key}"
            f"&url={urllib.parse.quote(url, safe='')}&render=true"
        )
        try:
            resp = requests.get(api_url, timeout=90)
            if resp.status_code == 403:
                print(f"[Careers] Key exhausted for {url}, trying next")
                continue
            if resp.status_code == 200:
                return resp.text
        except requests.RequestException as e:
            print(f"[Careers] Request failed for {url}: {e}")
            continue
    return None

def matches_role(text):
    lowered = text.lower()
    if any(kw in lowered for kw in EXCLUDE_KEYWORDS):
        return False
    return any(kw in lowered for kw in ROLE_KEYWORDS)

def extract_jobs(html, company):
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen = set()

    for tag in soup.find_all(['a', 'h2', 'h3', 'h4', 'li', 'div'], string=True):
        text = tag.get_text(strip=True)
        if len(text) < 5 or len(text) > 120:
            continue
        if not matches_role(text):
            continue

        href = tag.get('href', '') if tag.name == 'a' else ''
        if href and not href.startswith('http'):
            base = urllib.parse.urlparse(company['url'])
            href = f"{base.scheme}://{base.netloc}{href}"

        job_url = href or company['url']

        dedup_key = text.lower().strip()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        jobs.append({
            "title": text,
            "url": job_url,
            "company": company['name'],
        })

    return jobs

def scrape_careers():
    matches = []

    with database.get_connection() as conn:
        known_ids = {
            row["comment_id"]
            for row in conn.execute(
                "SELECT comment_id FROM jobs WHERE source = 'careers'"
            ).fetchall()
        }

        for company in COMPANIES:
            print(f"[Careers] Scraping {company['name']}...")
            html = fetch_page(company['url'])
            if not html:
                print(f"[Careers] Failed to fetch {company['name']}")
                continue

            jobs = extract_jobs(html, company)
            print(f"[Careers] {company['name']}: {len(jobs)} matching roles found")

            for job in jobs:
                combined = f"{job['title']}"
                min_years = experience_filter.parse_min_experience(combined)
                if not experience_filter.is_experience_allowed(min_years, combined):
                    continue

                job_id = url_to_id(job['url'], company['name'])
                if job_id in known_ids:
                    continue

                record = {
                    "comment_id": job_id,
                    "thread_id": 0,
                    "author": company['name'],
                    "posted_at": "",
                    "matched_keyword": job['title'],
                    "text": f"{job['title']} | Bangalore / Remote | via {company['name']} Careers",
                    "url": job['url'],
                    "company_url": company['url'],
                    "verified": True,
                    "source": "careers",
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
    results = scrape_careers()
    print(f"\nDirect Careers postings found: {len(results)}\n")
    for job in results:
        print(f"- [{job['author']}] {job['url']}")
        print(f"  {job['matched_keyword']}\n")

    changes, unresolved = fix_company_names.fix_names()
    if changes:
        print(f"fix_company_names: corrected {len(changes)} name(s):")
        for old_name, new_name in changes:
            print(f"  {old_name!r} -> {new_name!r}")
