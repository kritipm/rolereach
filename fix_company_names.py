import re
import urllib.parse

import requests

import config
import database

AGENCY_KEYWORDS = ["recruit", "staffing", "talent", "hiring", "solutions", "services", "manpower"]


def is_agency_name(name):
    lowered = (name or "").lower()
    return any(keyword in lowered for keyword in AGENCY_KEYWORDS)


def extract_company_from_hn_text(text):
    """HN 'Who is hiring' posts start with 'Company Name | Role | Location | Type'."""
    first_line = (text or "").split("\n", 1)[0]
    company_part = first_line.split("|", 1)[0].strip()
    company_part = re.sub(r"\s*\([^)]*\)\s*$", "", company_part).strip()
    return company_part or None


def extract_iimjobs_company_from_page(url):
    """iimjobs job-detail pages require JS rendering (bot-guarded), and their
    <title> tag follows the pattern '{Role} - {Company} | iimjobs.com'."""
    api_url = (
        f"{config.SCRAPERAPI_BASE_URL}?api_key={config.SCRAPERAPI_KEY}"
        f"&url={urllib.parse.quote(url, safe='')}&render=true"
    )
    try:
        resp = requests.get(api_url, timeout=90)
    except requests.RequestException:
        return None

    if resp.status_code != 200:
        return None

    match = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.DOTALL)
    if not match:
        return None

    title = match.group(1).split("|", 1)[0].strip()
    parts = title.rsplit(" - ", 1)
    if len(parts) == 2:
        return parts[1].strip()

    return None


def extract_real_employer(text, current_name):
    """Try to find the actual hiring company for an agency-posted listing.
    Returns None if the source text has no independent employer name to find."""
    if not text:
        return None

    # Look for an explicit "Client: X" / "Hiring for: X" marker, if present.
    match = re.search(r"(?:client|hiring for|on behalf of)\s*[:\-]\s*([A-Z][\w&.\- ]{2,40})", text, re.IGNORECASE)
    if match:
        candidate = match.group(1).strip()
        if candidate.lower() != current_name.lower():
            return candidate

    return None


def fix_names():
    with database.get_connection() as conn:
        rows = conn.execute("SELECT comment_id, source, author, text, url FROM jobs").fetchall()

        changes = []
        unresolved = []

        for row in rows:
            job = dict(row)
            old_name = job["author"]
            new_name = None

            if job["source"] == "hackernews":
                new_name = extract_company_from_hn_text(job["text"])
            elif job["source"] == "iimjobs" and (old_name or "").lower() == "unknown":
                new_name = extract_iimjobs_company_from_page(job["url"])
                if not new_name:
                    unresolved.append(old_name)
                    continue
            elif is_agency_name(old_name):
                new_name = extract_real_employer(job["text"], old_name)
                if not new_name:
                    unresolved.append(old_name)
                    continue

            if new_name and new_name != old_name:
                conn.execute("UPDATE jobs SET author = ? WHERE comment_id = ?", (new_name, job["comment_id"]))
                changes.append((old_name, new_name))

        conn.commit()

    return changes, unresolved


if __name__ == "__main__":
    database.init_db()
    changes, unresolved = fix_names()

    print(f"Changed {len(changes)} row(s):\n")
    for old_name, new_name in changes:
        print(f"  {old_name!r}  ->  {new_name!r}")

    if unresolved:
        print(f"\nCould not resolve real employer for {len(unresolved)} agency-named row(s) "
              f"(no independent company name anywhere in the stored data):")
        for name in unresolved:
            print(f"  {name!r}  (left unchanged)")
