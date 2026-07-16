import os
import re
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import config
import database

load_dotenv()

SNOV_ACCOUNTS = [
    (os.environ["SNOV_CLIENT_ID"], os.environ["SNOV_CLIENT_SECRET"]),
    (os.environ["SNOV_CLIENT_ID_2"], os.environ["SNOV_CLIENT_SECRET_2"]),
    (os.environ["SNOV_CLIENT_ID_3"], os.environ["SNOV_CLIENT_SECRET_3"]),
]

TOKEN_URL = "https://api.snov.io/v1/oauth/access_token"
PROSPECTS_START_URL = "https://api.snov.io/v2/domain-search/prospects/start"

HM_TITLE_KEYWORDS = ["product", "founder", "ceo", "chief executive", "hiring"]
POLL_ATTEMPTS = 6
POLL_DELAY_SECONDS = 3

GENERIC_EMAIL_PREFIXES = [
    "hr", "namaste", "sales", "help", "care", "info", "support", "hello", "contact",
    "admin", "team", "careers", "jobs", "recruitment", "hiring", "talent", "noreply",
    "no-reply", "enquiry", "enquiries", "office", "accounts", "billing",
]
EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
WEBSITE_SCRAPE_PATHS = ["", "/about", "/about-us", "/contact", "/contact-us", "/team"]

# Non-email junk that the regex can still technically match: retina image
# filenames like "logo@2x.png" and template placeholder text like "your@email.com".
IMAGE_FILE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp"}
PLACEHOLDER_EMAILS = {
    "your@email.com", "you@example.com", "name@example.com", "email@example.com",
    "test@test.com", "example@example.com", "user@example.com",
    "john@example.com", "jane@example.com", "someone@example.com",
}
# "firstname.lastname@gmail.com" / "@yahoo.com" is a classic template placeholder
# pattern (e.g. john.doe@gmail.com) rather than a real scraped contact.
TEMPLATE_PERSONAL_EMAIL_PATTERN = re.compile(r"^[a-z]+\.[a-z]+@(gmail|yahoo)\.com$", re.IGNORECASE)


class SnovCreditsExhausted(Exception):
    """Raised specifically on HTTP 402 — the account has run out of credits."""


# ---------- Step 1: Snov named-contact lookup (rotates across 3 accounts) ----------


def get_access_token(client_id, client_secret):
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def extract_domain(company_url):
    parsed = urllib.parse.urlparse(company_url if "://" in company_url else f"//{company_url}")
    domain = parsed.netloc or parsed.path
    return domain.split("@")[-1].removeprefix("www.").split("/")[0]


def fetch_prospects(domain, token):
    """Poll Snov's async v2 domain-search/prospects job. Returns a list of
    prospect dicts (each with name/position/search_emails_start) or [] if none
    are indexed for this domain. Raises SnovCreditsExhausted on 402."""
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(PROSPECTS_START_URL, headers=headers, json={"domain": domain}, timeout=30)
    if resp.status_code == 402:
        raise SnovCreditsExhausted()
    resp.raise_for_status()

    result_url = resp.json().get("links", {}).get("result")
    if not result_url:
        return []

    for _ in range(POLL_ATTEMPTS):
        result_resp = requests.get(result_url, headers=headers, timeout=30)
        if result_resp.status_code == 402:
            raise SnovCreditsExhausted()
        result_resp.raise_for_status()
        payload = result_resp.json()
        if payload.get("status") == "completed":
            return payload.get("data") or []
        time.sleep(POLL_DELAY_SECONDS)

    return []


def matches_hm_title(prospect):
    title = (prospect.get("position") or "").lower()
    return any(keyword in title for keyword in HM_TITLE_KEYWORDS)


def reveal_email(prospect, token):
    """Raises SnovCreditsExhausted on 402; returns None for any other failure."""
    start_url = prospect.get("search_emails_start")
    if not start_url:
        return None

    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.post(start_url, headers=headers, timeout=30)
        if resp.status_code == 402:
            raise SnovCreditsExhausted()
        resp.raise_for_status()
        result_url = resp.json().get("links", {}).get("result")
        if not result_url:
            return None

        for _ in range(POLL_ATTEMPTS):
            result_resp = requests.get(result_url, headers=headers, timeout=30)
            if result_resp.status_code == 402:
                raise SnovCreditsExhausted()
            result_resp.raise_for_status()
            payload = result_resp.json()
            if payload.get("status") == "completed":
                emails = (payload.get("data") or {}).get("emails") or []
                return emails[0]["email"] if emails else None
            time.sleep(POLL_DELAY_SECONDS)
    except SnovCreditsExhausted:
        raise
    except requests.RequestException:
        return None

    return None


def try_snov_account(domain, client_id, client_secret):
    """Returns (name, email, credits_exhausted). credits_exhausted=True means the
    caller should rotate to the next account; False means this account had capacity
    but genuinely found nothing (no point trying the others for the same domain)."""
    try:
        token = get_access_token(client_id, client_secret)
    except requests.RequestException:
        return None, None, False

    try:
        prospects = fetch_prospects(domain, token)
    except SnovCreditsExhausted:
        return None, None, True
    except requests.RequestException:
        prospects = []

    title_matches = [p for p in prospects if matches_hm_title(p)]
    for prospect in title_matches:
        try:
            email = reveal_email(prospect, token)
        except SnovCreditsExhausted:
            return None, None, True

        if email:
            name = " ".join(filter(None, [prospect.get("first_name"), prospect.get("last_name")]))
            return name or None, email, False

    return None, None, False


def find_named_email_rotating(domain):
    """Try Account 1, then 2, then 3, on 402 only. Returns (name, email, account_number)."""
    for account_number, (client_id, client_secret) in enumerate(SNOV_ACCOUNTS, start=1):
        name, email, exhausted = try_snov_account(domain, client_id, client_secret)
        if name and email:
            return name, email, account_number
        if not exhausted:
            break  # this account had capacity and genuinely found nothing — stop here

    return None, None, None


# ---------- Fallback: scrape company website /about /contact /team for a real email ----------


def is_valid_real_email(email):
    lowered = email.lower()

    if lowered in PLACEHOLDER_EMAILS:
        return False
    if TEMPLATE_PERSONAL_EMAIL_PATTERN.match(lowered):
        return False
    if "%" in email or " " in email:
        return False

    local_part, _, domain_part = lowered.partition("@")
    if any(local_part.startswith(prefix) for prefix in GENERIC_EMAIL_PREFIXES):
        return False

    if domain_part.endswith(".gov") or ".gov." in domain_part:
        return False

    tld = domain_part.rsplit(".", 1)[-1]
    if tld in IMAGE_FILE_EXTENSIONS:
        return False
    if domain_part in ("example.com", "domain.com", "yourdomain.com", "yoursite.com"):
        return False

    return True


def scrape_website_email(company_url):
    if not company_url:
        return None

    base = company_url.rstrip("/")
    for path in WEBSITE_SCRAPE_PATHS:
        try:
            resp = requests.get(base + path, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        except requests.RequestException:
            continue

        if resp.status_code != 200:
            continue

        candidates = [e for e in EMAIL_PATTERN.findall(resp.text) if is_valid_real_email(e)]
        if candidates:
            return candidates[0]

    return None


# ---------- Step: company LinkedIn via Google search (ScraperAPI) — always attempted ----------


def derive_company_name(job, domain):
    author = (job.get("author") or "").strip()
    if author and author.lower() != "unknown":
        return author
    base = domain.split(".")[0]
    return base.replace("-", " ").replace("_", " ").title()


def search_company_linkedin(company_name):
    query = f'"{company_name}" site:linkedin.com/company'
    search_url = f"https://www.google.com/search?q={urllib.parse.quote(query)}&num=10"
    api_url = (
        f"{config.SCRAPERAPI_BASE_URL}?api_key={config.SCRAPERAPI_KEY}"
        f"&url={urllib.parse.quote(search_url, safe='')}"
    )

    resp = requests.get(api_url, timeout=30)
    if resp.status_code != 200:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not href.startswith("http"):
            continue

        parsed = urllib.parse.urlparse(href)
        # Check the link's actual domain/path, not a substring match anywhere in
        # the URL — Google's own ServiceLogin link embeds the raw search query
        # (which contains "linkedin.com/company" as literal text) in its
        # `continue=` parameter, which a naive substring check would wrongly match.
        if parsed.netloc.endswith("linkedin.com") and parsed.path.startswith("/company/"):
            return href.split("&")[0].split("#")[0]

    return None


# ---------- Orchestration ----------


def enrich_job(job):
    domain = extract_domain(job["company_url"])

    name, email, account_number = find_named_email_rotating(domain)

    category = None
    if name and email:
        database.update_hiring_manager(job["comment_id"], name, email)
        category = f"snov_account{account_number}"
    else:
        scraped_email = scrape_website_email(job["company_url"])
        if scraped_email:
            database.update_hiring_manager(job["comment_id"], None, scraped_email)
            category = "website_scrape"

    # LinkedIn is always attempted regardless of whether an email was found.
    company_name = derive_company_name(job, domain)
    try:
        linkedin_url = search_company_linkedin(company_name)
    except requests.RequestException:
        linkedin_url = None

    if linkedin_url:
        database.update_company_linkedin(job["comment_id"], linkedin_url)

    if category is None:
        category = "linkedin_only" if linkedin_url else "nothing"

    return {"job_url": job["url"], "domain": domain, "category": category}


def enrich_all():
    jobs = database.fetch_jobs_with_company_url()
    results = []

    for job in jobs:
        if job.get("hm_email"):
            results.append({"job_url": job["url"], "domain": extract_domain(job["company_url"]), "category": "already_had_email"})
            continue

        results.append(enrich_job(job))

    return results


if __name__ == "__main__":
    database.init_db()
    results = enrich_all()

    counts = {}
    for result in results:
        counts[result["category"]] = counts.get(result["category"], 0) + 1
        print(f"- {result['job_url']}  [{result['category']}]")

    print()
    print(f"Total jobs processed: {len(results)}")
    for category, count in sorted(counts.items()):
        print(f"  {category}: {count}")
