import os
import requests
import config
import database

APOLLO_API_URL = "https://api.apollo.io/v1/people/match"

def get_apollo_headers():
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }

def find_email_apollo(company_url, company_name):
    """Try to find a PM/founder email at the company using Apollo people match."""
    if not config.APOLLO_API_KEY:
        return None, None

    # Try different titles in priority order
    titles_to_try = [
        "Product Manager",
        "Associate Product Manager",
        "Founder",
        "Co-Founder",
        "CEO",
        "CTO",
        "Head of Product",
    ]

    domain = None
    if company_url:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(company_url)
            domain = parsed.netloc.replace("www.", "")
        except Exception:
            pass

    if not domain and not company_name:
        return None, None

    for title in titles_to_try:
        payload = {
            "api_key": config.APOLLO_API_KEY,
            "organization_name": company_name,
            "title": title,
        }
        if domain:
            payload["domain"] = domain

        try:
            resp = requests.post(
                APOLLO_API_URL,
                json=payload,
                headers=get_apollo_headers(),
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                person = data.get("person")
                if person:
                    email = person.get("email")
                    name = person.get("name")
                    if email and "apollo.io" not in email:
                        print(f"[Apollo] Found {name} <{email}> at {company_name} ({title})")
                        return name, email
        except requests.RequestException as e:
            print(f"[Apollo] Request failed for {company_name}: {e}")
            continue

    return None, None


def enrich_with_apollo():
    """Enrich jobs that have no email yet using Apollo."""
    jobs = database.fetch_jobs_needing_enrichment()
    enriched = 0

    for job in jobs:
        company_url = job.get("company_url") or ""
        company_name = job.get("author") or ""

        if not company_url and not company_name:
            continue

        name, email = find_email_apollo(company_url, company_name)
        if email:
            database.update_hiring_manager(job["comment_id"], name, email)
            enriched += 1

    return enriched


if __name__ == "__main__":
    database.init_db()
    count = enrich_with_apollo()
    print(f"Apollo enrichment: {count} jobs enriched with email")
