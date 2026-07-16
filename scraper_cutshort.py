import html
import json
import re

import requests

import config
import database
import experience_filter
import fix_company_names

NEXT_DATA_PATTERN = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)


def fetch_page_jobs(page):
    resp = requests.get(
        config.CUTSHORT_CATEGORY_URL,
        params={"page": page},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=config.REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()

    match = NEXT_DATA_PATTERN.search(resp.text)
    if not match:
        return []

    data = json.loads(match.group(1))
    try:
        queries = data["props"]["pageProps"]["dehydratedState"]["queries"]
        return queries[0]["state"]["data"]["data"]["pageData"]["jobs"]
    except (KeyError, IndexError, TypeError):
        return []


def strip_html_tags(raw_html):
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def fetch_job_description(public_url):
    """Visit the job's own detail page and pull its full description text
    (job listing pages only expose the bare headline)."""
    if not public_url:
        return None

    try:
        resp = requests.get(public_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=config.REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    match = NEXT_DATA_PATTERN.search(resp.text)
    if not match:
        return None

    try:
        data = json.loads(match.group(1))
        queries = data["props"]["pageProps"]["dehydratedState"]["queries"]
        raw_comment = queries[0]["state"]["data"]["data"]["pageData"]["sanitizedComment"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return None

    return strip_html_tags(raw_comment) or None


def matches_role(headline):
    lowered = headline.lower()
    if any(keyword in lowered for keyword in config.TITLE_EXCLUDE_KEYWORDS):
        return False
    return any(keyword in lowered for keyword in config.CUTSHORT_ROLE_KEYWORDS)


def is_india_or_remote(job):
    if job.get("remoteType") == "remote_only":
        return True

    locations_text = (job.get("locationsText") or "").lower()
    if "india" in locations_text:
        return True

    if any(marker in locations_text for marker in config.CUTSHORT_FOREIGN_LOCATION_MARKERS):
        return False

    # Cutshort is an India-focused board; an onsite location with no foreign marker
    # is assumed to be an Indian city.
    return True


def to_comment_id(external_id):
    """Fold Cutshort's 24-char hex object id into a positive 63-bit int for the shared PK."""
    return int(external_id[:15], 16) & 0x7FFFFFFFFFFFFFFF


def format_experience_range(exp_range):
    min_years = exp_range.get("min")
    max_years = exp_range.get("max")
    if min_years is None:
        return "Not specified"
    if max_years is None or max_years == min_years:
        return f"{min_years} years"
    return f"{min_years}-{max_years} years"


def build_job(job, page, description):
    headline = html.unescape(job.get("headline") or "")
    company = job.get("companyDetails") or {}
    company_url = (company.get("links") or {}).get("website")
    verified = company.get("stage") == "Raised funding"
    external_id = job["_id"]

    return {
        "comment_id": to_comment_id(external_id),
        "thread_id": page,
        "author": company.get("name", "unknown"),
        "posted_at": "",
        "matched_keyword": headline,
        "text": headline,
        "url": job.get("publicUrl", ""),
        "company_url": company_url,
        "verified": verified,
        "source": "cutshort",
        "external_id": external_id,
        "experience_range": format_experience_range(job.get("expRange") or {}),
        "description": description,
    }


def scrape_cutshort_pm_jobs():
    matches = []
    with database.get_connection() as conn:
        for page in range(1, config.CUTSHORT_PAGES_TO_SCAN + 1):
            try:
                jobs = fetch_page_jobs(page)
            except requests.RequestException:
                continue

            if not jobs:
                break

            for job in jobs:
                headline = job.get("headline") or ""
                if not matches_role(headline):
                    continue
                if not is_india_or_remote(job):
                    continue
                if not experience_filter.has_product_in_title(headline):
                    continue

                description = fetch_job_description(job.get("publicUrl"))
                combined_text = f"{headline} {description or ''}"

                min_years = (job.get("expRange") or {}).get("min")
                if not experience_filter.is_experience_allowed(min_years, combined_text):
                    continue

                record = build_job(job, page, description)
                database.save_job(conn, record)
                matches.append(record)

        conn.commit()

    return matches


if __name__ == "__main__":
    database.init_db()
    results = scrape_cutshort_pm_jobs()
    print(f"Cutshort PM postings found: {len(results)}\n")
    for job in results:
        verified_label = "Yes" if job["verified"] else "No"
        print(f"- [{job['author']}] {job['url']}")
        print(f"  title: {job['matched_keyword']}")
        print(f"  company_url: {job['company_url'] or 'N/A'}")
        print(f"  verified: {verified_label}\n")

    changes, unresolved = fix_company_names.fix_names()
    if changes:
        print(f"fix_company_names: corrected {len(changes)} name(s):")
        for old_name, new_name in changes:
            print(f"  {old_name!r} -> {new_name!r}")
