import hashlib
from datetime import datetime, timezone, timedelta

import requests

import config
import database
import fix_company_names

GREENHOUSE_COMPANIES = {
    "Freshworks": "freshworks", "Chargebee": "chargebee", "BrowserStack": "browserstack", "Postman": "postmanapp", "CleverTap": "clevertap", "Darwinbox": "darwinbox", "Innovaccer": "innovaccer", "CRED": "cred", "Smallcase": "smallcase", "WebEngage": "webengage", "Wingify": "wingify", "Whatfix": "whatfix", "LeadSquared": "leadsquared", "HackerEarth": "hackerearth", "Unacademy": "unacademy", "Scaler": "scaler", "Classplus": "classplus", "Practo": "practo", "Delhivery": "delhivery", "Shiprocket": "shiprocket", "Porter": "porter", "upGrad": "upgrad", "MoEngage": "moengage", "Netcore Cloud": "netcore", "Pristyn Care": "pristyncare", "Exotel": "exotel"
}

LEVER_COMPANIES = {
    "Razorpay": "razorpay", "Groww": "groww", "Meesho": "meesho", "Urban Company": "urbancompany", "Swiggy": "swiggy", "Zomato": "zomato", "Rapido": "rapido", "Jupiter": "jupiter-1", "Setu": "setu", "ShareChat": "sharechat", "Pocket FM": "pocketfm", "Kuku FM": "kukufm", "Inshorts": "inshorts", "Physics Wallah": "physicswallah", "BharatPe": "bharatpe", "Juspay": "juspay", "Nykaa": "nykaa", "KreditBee": "kreditbee", "PhonePe": "phonepe", "Niyo": "niyo", "Slice": "sliceit", "Lenskart": "lenskart", "Perfios": "perfios"
}

TITLE_INCLUDE_KEYWORDS = [
    "product manager",
    "associate product manager",
    "apm",
    "product analyst",
    "growth pm",
    "ai pm",
    "founders office",
    "founder's office",
    "chief of staff",
    "product owner",
    "product lead",
]

TITLE_EXCLUDE_KEYWORDS = [
    "senior",
    "sr.",
    "director",
    "vp ",
    "vice president",
    "head of",
    "principal",
]


def matches_title(title):
    lowered = title.lower()
    if any(keyword in lowered for keyword in TITLE_EXCLUDE_KEYWORDS):
        return False
    return any(keyword in lowered for keyword in TITLE_INCLUDE_KEYWORDS)


def is_recently_posted(posted_date, days=7):
    """True if posted_date is within the last `days` days. Unknown date = include."""
    if posted_date is None:
        return True
    if posted_date.tzinfo is None:
        posted_date = posted_date.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - posted_date) <= timedelta(days=days)


def to_comment_id(source_platform, external_id):
    """Hash a platform-scoped external id into a positive 63-bit int for the shared PK."""
    raw = f"{source_platform}:{external_id}"
    return int.from_bytes(hashlib.sha256(raw.encode()).digest()[:8], "big") & 0x7FFFFFFFFFFFFFFF


def build_job(company, title, location, url, posted_date, external_id, source_platform):
    return {
        "comment_id": to_comment_id(source_platform, external_id),
        "thread_id": 0,
        "author": company,
        "posted_at": posted_date.isoformat() if posted_date else "",
        "matched_keyword": title,
        "text": f"{title} | {location or 'India'} | via {source_platform.title()}",
        "url": url,
        "company_url": None,
        "verified": False,
        "source": "careers_direct",
        "external_id": str(external_id),
        "experience_range": "Not specified",
        "description": None,
    }


def fetch_greenhouse_jobs(company, slug):
    url = f"https://boards.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        resp = requests.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException:
        print(f"[Careers] {company} | ATS not reachable, skipping")
        return []

    if resp.status_code != 200:
        print(f"[Careers] {company} | ATS not reachable, skipping")
        return []

    try:
        data = resp.json()
    except ValueError:
        print(f"[Careers] {company} | ATS not reachable, skipping")
        return []

    jobs = data.get("jobs", [])
    matched = []

    for job in jobs:
        title = job.get("title") or ""
        if not matches_title(title):
            continue

        posted_date = None
        updated_at = job.get("updated_at")
        if updated_at:
            try:
                posted_date = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            except ValueError:
                posted_date = None
        if not is_recently_posted(posted_date):
            continue

        location = (job.get("location") or {}).get("name", "India")
        record = build_job(
            company=company,
            title=title,
            location=location,
            url=job.get("absolute_url", ""),
            posted_date=posted_date,
            external_id=str(job["id"]),
            source_platform="greenhouse",
        )
        matched.append(record)

    print(f"[Careers] {company} | greenhouse | {len(jobs)} total | {len(matched)} matched")
    return matched


def fetch_lever_jobs(company, slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        resp = requests.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException:
        print(f"[Careers] {company} | ATS not reachable, skipping")
        return []

    if resp.status_code != 200:
        print(f"[Careers] {company} | ATS not reachable, skipping")
        return []

    try:
        jobs = resp.json()
    except ValueError:
        print(f"[Careers] {company} | ATS not reachable, skipping")
        return []

    if not isinstance(jobs, list):
        print(f"[Careers] {company} | ATS not reachable, skipping")
        return []

    matched = []

    for job in jobs:
        title = job.get("text") or ""
        if not matches_title(title):
            continue

        posted_date = None
        created_at = job.get("createdAt")
        if created_at is not None:
            try:
                posted_date = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                posted_date = None
        if not is_recently_posted(posted_date):
            continue

        location = (job.get("categories") or {}).get("location", "India")
        record = build_job(
            company=company,
            title=title,
            location=location,
            url=job.get("hostedUrl", ""),
            posted_date=posted_date,
            external_id=str(job.get("id")),
            source_platform="lever",
        )
        matched.append(record)

    print(f"[Careers] {company} | lever | {len(jobs)} total | {len(matched)} matched")
    return matched


def scrape_careers_pm_jobs():
    matches = []
    with database.get_connection() as conn:
        known_ids = {
            row["comment_id"]
            for row in conn.execute(
                "SELECT comment_id FROM jobs WHERE source = 'careers_direct'"
            ).fetchall()
        }

        for company, slug in GREENHOUSE_COMPANIES.items():
            for record in fetch_greenhouse_jobs(company, slug):
                if record["comment_id"] in known_ids:
                    continue
                database.save_job(conn, record)
                known_ids.add(record["comment_id"])
                matches.append(record)

        for company, slug in LEVER_COMPANIES.items():
            for record in fetch_lever_jobs(company, slug):
                if record["comment_id"] in known_ids:
                    continue
                database.save_job(conn, record)
                known_ids.add(record["comment_id"])
                matches.append(record)

        conn.commit()

    return matches


if __name__ == "__main__":
    database.init_db()
    results = scrape_careers_pm_jobs()
    print(f"\nDirect-ATS PM postings found: {len(results)}\n")
    for job in results:
        print(f"- [{job['author']}] {job['url']}")
        print(f"  {job['matched_keyword']}\n")

    changes, unresolved = fix_company_names.fix_names()
    if changes:
        print(f"fix_company_names: corrected {len(changes)} name(s):")
        for old_name, new_name in changes:
            print(f"  {old_name!r} -> {new_name!r}")
