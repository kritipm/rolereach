import hashlib
import re
from datetime import datetime, timezone, timedelta

import requests

import config
import database
import fix_company_names

# --- Slug-hunt results (see check_ats_slugs.py + live careers-page probe) ---
# Every company below was checked against Greenhouse, Lever, and (via its public
# careers page) Workday / Darwinbox / SmartRecruiters / Freshteam signatures.
# Only the two already confirmed live on Lever remain active here.
GREENHOUSE_COMPANIES = {
    "Razorpay": "razorpaysoftwareprivatelimited",  # confirmed live 2026-08-13 — 22 postings
    # "Chargebee": "chargebee",     # Not on accessible ATS
    # "Postman": "postmanapp",      # Not on accessible ATS
    # "CleverTap": "clevertap",     # Darwinbox SPA - requires headless browser, skipping
    # "Darwinbox": "darwinbox",     # Not on accessible ATS (no confirmed public jobs feed of their own)
    # "Innovaccer": "innovaccer",   # Not on accessible ATS
    # "CRED": "cred",               # Not on accessible ATS
    # "Smallcase": "smallcase",     # On PyjamaHR (app.pyjamahr.com) — not one of the checked platforms, not integrated
    # "WebEngage": "webengage",     # Not on accessible ATS
    # "Wingify": "wingify",         # Not on accessible ATS
    # "Whatfix": "whatfix",         # Not on accessible ATS
    # "LeadSquared": "leadsquared", # Not on accessible ATS
    # "HackerEarth": "hackerearth", # Not on accessible ATS
    # "Unacademy": "unacademy",     # Not on accessible ATS (careers subdomain redirects to a Wellfound listing, not a feed)
    # "Scaler": "scaler",           # Not on accessible ATS
    # "Classplus": "classplus",     # Not on accessible ATS (careers domain unreachable)
    # "Practo": "practo",           # On Param.ai (practo.app.param.ai) — not one of the checked platforms, not integrated
    # "Delhivery": "delhivery",     # Not on accessible ATS
    # "Shiprocket": "shiprocket",   # Not on accessible ATS
    # "Porter": "porter",           # Confirmed on Darwinbox: https://porter.darwinbox.in/ms/candidate/careers — not integrated (no Darwinbox scraper built)
    # "upGrad": "upgrad",           # Not on accessible ATS
    # "MoEngage": "moengage",       # Not on accessible ATS
    # "Netcore Cloud": "netcore",   # Not on accessible ATS
    # "Pristyn Care": "pristyncare",  # Not on accessible ATS
    # "Exotel": "exotel",           # Not on accessible ATS
}

LEVER_COMPANIES = {
    "Meesho": "meesho",
    "Pocket FM": "pocketfm",
    # "Razorpay": "razorpay",       # Not on accessible ATS
    # "Groww": "groww",             # Not on accessible ATS
    # "Urban Company": "urbancompany",  # Not on accessible ATS (custom careers page)
    # "Swiggy": "swiggy",           # Not on accessible ATS (custom careers page)
    # "Zomato": "zomato",           # Not on accessible ATS (redirects to eternal.com/careers, custom page)
    # "Rapido": "rapido",           # Not on accessible ATS
    # "Jupiter": "jupiter-1",       # Not on accessible ATS
    # "Setu": "setu",               # Not on accessible ATS
    # "ShareChat": "sharechat",     # Not on accessible ATS
    # "Kuku FM": "kukufm",          # Not on accessible ATS
    # "Inshorts": "inshorts",       # Not on accessible ATS
    # "Physics Wallah": "physicswallah",  # Not on accessible ATS
    # "BharatPe": "bharatpe",       # Not on accessible ATS
    # "Juspay": "juspay",           # Not on accessible ATS
    # "Nykaa": "nykaa",             # Not on accessible ATS
    # "KreditBee": "kreditbee",     # Not on accessible ATS
    # "PhonePe": "phonepe",         # Not on accessible ATS
    # "Niyo": "niyo",               # Not on accessible ATS
    # "Slice": "sliceit",           # Not on accessible ATS
    # "Lenskart": "lenskart",       # On ainterviews.com (job_board/lenskart_ho) — not one of the checked platforms, not integrated
    # "Perfios": "perfios",         # Not on accessible ATS
}

# Workday tenants confirmed via a live careers-page probe. "pool" is the tenant's
# pod number (wd1/wd3/wd5/...) — differs per company, read off their careers URL.
WORKDAY_COMPANIES = {
    "BrowserStack": {"subdomain": "browserstack", "jobsite": "External", "pool": "wd3"},  # confirmed live 2026-08-13 — 35 postings
}

# SmartRecruiters companies confirmed via a live careers-page probe. Identifier
# is SmartRecruiters' own company slug (case-sensitive, as it appears in their URL).
SMARTRECRUITERS_COMPANIES = {
    "Freshworks": "Freshworks",  # confirmed live 2026-08-13 — https://careers.smartrecruiters.com/Freshworks
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


def fetch_smartrecruiters_jobs(company, identifier):
    url = f"https://api.smartrecruiters.com/v1/companies/{identifier}/postings?limit=100"
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

    postings = data.get("content", [])
    matched = []

    for job in postings:
        title = job.get("name") or ""
        if not matches_title(title):
            continue

        posted_date = None
        released_date = job.get("releasedDate")
        if released_date:
            try:
                posted_date = datetime.fromisoformat(released_date.replace("Z", "+00:00"))
            except ValueError:
                posted_date = None
        if not is_recently_posted(posted_date):
            continue

        job_id = job.get("id")
        record = build_job(
            company=company,
            title=title,
            location=(job.get("location") or {}).get("city", "India"),
            url=f"https://jobs.smartrecruiters.com/{identifier}/{job_id}",
            posted_date=posted_date,
            external_id=job_id,
            source_platform="smartrecruiters",
        )
        matched.append(record)

    print(f"[Careers] {company} | smartrecruiters | {len(postings)} total | {len(matched)} matched")
    return matched


def parse_workday_posted_on(text):
    """Workday's `postedOn` is relative text ("Posted Today", "Posted 5 Days Ago",
    "Posted 30+ Days Ago"), not an ISO timestamp — approximate a UTC datetime from it.
    Unparseable/missing text = None (treated as "unknown date = include" downstream)."""
    if not text:
        return None
    lowered = text.lower()
    if "today" in lowered:
        return datetime.now(timezone.utc)
    if "yesterday" in lowered:
        return datetime.now(timezone.utc) - timedelta(days=1)
    match = re.search(r"(\d+)\+?\s*day", lowered)
    if match:
        return datetime.now(timezone.utc) - timedelta(days=int(match.group(1)))
    return None


def fetch_workday_jobs(company, subdomain, jobsite, pool="wd5"):
    """Workday's public CXS endpoint requires POST (a plain GET 400s/401s), with a
    body of {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""} —
    confirmed against a live Workday tenant. `pool` is the tenant's pod number
    (wd1/wd3/wd5/...), which differs per company and isn't guessable from the name."""
    url = f"https://{subdomain}.{pool}.myworkdayjobs.com/wday/cxs/{subdomain}/{jobsite}/jobs"
    payload = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

    try:
        resp = requests.post(url, json=payload, timeout=config.REQUEST_TIMEOUT_SECONDS)
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

    postings = data.get("jobPostings", [])
    matched = []

    for job in postings:
        title = job.get("title") or ""
        if not matches_title(title):
            continue

        posted_date = parse_workday_posted_on(job.get("postedOn"))
        if not is_recently_posted(posted_date):
            continue

        # externalPath already begins with "/job/..." — the browsable URL is the
        # jobsite root plus that path, not "/job/" + externalPath (would double up).
        external_path = job.get("externalPath") or ""
        record = build_job(
            company=company,
            title=title,
            location=job.get("locationsText") or "India",
            url=f"https://{subdomain}.{pool}.myworkdayjobs.com/{jobsite}{external_path}",
            posted_date=posted_date,
            external_id=external_path or title,
            source_platform="workday",
        )
        matched.append(record)

    print(f"[Careers] {company} | workday | {len(postings)} total | {len(matched)} matched")
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

        for company, identifier in SMARTRECRUITERS_COMPANIES.items():
            for record in fetch_smartrecruiters_jobs(company, identifier):
                if record["comment_id"] in known_ids:
                    continue
                database.save_job(conn, record)
                known_ids.add(record["comment_id"])
                matches.append(record)

        for company, workday_info in WORKDAY_COMPANIES.items():
            jobs = fetch_workday_jobs(
                company,
                subdomain=workday_info["subdomain"],
                jobsite=workday_info["jobsite"],
                pool=workday_info.get("pool", "wd5"),
            )
            for record in jobs:
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
