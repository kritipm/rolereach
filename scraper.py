import html
import re
from datetime import datetime, timezone

import requests

import config
import database
import experience_filter


def find_latest_hiring_thread():
    """Find the most recent 'Ask HN: Who is hiring?' story via Algolia's HN search API."""
    params = {
        "tags": f"story,author_{config.HIRING_THREAD_AUTHOR}",
        "query": "Who is Hiring",
        "hitsPerPage": 5,
    }
    resp = requests.get(
        config.ALGOLIA_SEARCH_URL, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS
    )
    resp.raise_for_status()
    hits = resp.json().get("hits", [])

    for hit in hits:
        title = (hit.get("title") or "").lower()
        if config.HIRING_THREAD_TITLE_HINT in title:
            return int(hit["objectID"]), hit.get("title")

    raise RuntimeError("Could not find a 'Who is hiring?' thread")


def fetch_item(item_id):
    url = config.FIREBASE_ITEM_URL.format(id=item_id)
    resp = requests.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def strip_html(raw_text):
    text = re.sub(r"<p>", "\n", raw_text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def match_pm_keyword(text):
    lowered = f" {text.lower()} "
    for keyword in config.PM_KEYWORDS:
        if keyword in lowered:
            return keyword
    return None


def is_excluded_title(text):
    """Titles/roles are listed in the first line, e.g. 'Company | Role | Location'."""
    title_line = text.split("\n", 1)[0].lower()
    return any(keyword in title_line for keyword in config.TITLE_EXCLUDE_KEYWORDS)


def extract_company_name(text):
    """HN 'Who is hiring' posts start with 'Company Name | Role | Location | Type'."""
    first_line = text.split("\n", 1)[0]
    company_part = first_line.split("|", 1)[0].strip()
    company_part = re.sub(r"\s*\([^)]*\)\s*$", "", company_part).strip()
    return company_part or None


def extract_company_url(text):
    match = re.search(r"https?://\S+", text)
    if not match:
        return None
    return match.group(0).rstrip(").,;")


def is_verified(text):
    lowered = text.lower()
    return any(keyword in lowered for keyword in config.FUNDING_KEYWORDS)


def is_location_allowed(text):
    """Keep: remote (any/no-country-restriction), remote-open-to-India, India-based, or global.
    Drop: explicit US/Canada/EU-only restrictions, or onsite roles outside India."""
    lowered = text.lower()

    if any(re.search(pattern, lowered) for pattern in config.LOCATION_REJECT_PATTERNS):
        return False

    if "onsite" in lowered and "india" not in lowered:
        return False

    return any(keyword in lowered for keyword in config.LOCATION_ALLOW_KEYWORDS)


def scrape_pm_jobs():
    thread_id, thread_title = find_latest_hiring_thread()
    thread = fetch_item(thread_id)
    comment_ids = (thread.get("kids") or [])[: config.MAX_COMMENTS_TO_SCAN]

    matches = []
    with database.get_connection() as conn:
        for comment_id in comment_ids:
            try:
                comment = fetch_item(comment_id)
            except requests.RequestException:
                continue

            if not comment or comment.get("dead") or comment.get("deleted"):
                continue

            raw_text = comment.get("text") or ""
            if not raw_text:
                continue

            text = strip_html(raw_text)
            keyword = match_pm_keyword(text)
            if not keyword:
                continue

            if is_excluded_title(text):
                continue

            if not is_location_allowed(text):
                continue

            title_line = text.split("\n", 1)[0]
            if not experience_filter.has_product_in_title(title_line):
                continue

            min_years = experience_filter.parse_min_experience(text)
            if not experience_filter.is_experience_allowed(min_years, text):
                continue

            posted_at = datetime.fromtimestamp(
                comment.get("time", 0), tz=timezone.utc
            ).isoformat()

            job = {
                "comment_id": comment["id"],
                "thread_id": thread_id,
                "author": extract_company_name(text) or comment.get("by", "unknown"),
                "posted_at": posted_at,
                "matched_keyword": keyword.strip(),
                "text": text,
                "url": f"https://news.ycombinator.com/item?id={comment['id']}",
                "company_url": extract_company_url(text),
                "verified": is_verified(text),
                "experience_range": experience_filter.extract_experience_range(text),
            }
            database.save_job(conn, job)
            matches.append(job)

        conn.commit()

    return thread_title, thread_id, matches
