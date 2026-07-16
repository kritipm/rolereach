import os
import urllib.parse

import requests
from dotenv import load_dotenv

import database

load_dotenv()

APOLLO_API_KEY = os.environ["APOLLO_API_KEY"]
APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/search"
HM_TITLES = ["product", "founder", "ceo"]


def extract_domain(company_url):
    parsed = urllib.parse.urlparse(company_url if "://" in company_url else f"//{company_url}")
    domain = parsed.netloc or parsed.path
    return domain.split("@")[-1].removeprefix("www.").split("/")[0]


def find_hiring_manager(domain):
    headers = {"Content-Type": "application/json", "x-api-key": APOLLO_API_KEY}
    body = {
        "q_organization_domains": domain,
        "person_titles": HM_TITLES,
        "page": 1,
        "per_page": 5,
    }
    resp = requests.post(APOLLO_SEARCH_URL, headers=headers, json=body, timeout=20)

    if resp.status_code != 200:
        return None, None, f"HTTP {resp.status_code}: {resp.text[:200]}"

    people = resp.json().get("people", [])
    if not people:
        return None, None, "no matching people found"

    person = people[0]
    name = person.get("name")
    email = person.get("email")
    return name, email, None


def enrich_jobs(limit=5):
    jobs = database.fetch_jobs_needing_enrichment(limit=limit)
    results = []

    for job in jobs:
        domain = extract_domain(job["company_url"])
        name, email, error = find_hiring_manager(domain)

        if name and email:
            database.update_hiring_manager(job["comment_id"], name, email)

        results.append(
            {
                "job_url": job["url"],
                "company_url": job["company_url"],
                "domain": domain,
                "hm_name": name,
                "hm_email": email,
                "error": error,
            }
        )

    return results


if __name__ == "__main__":
    database.init_db()
    for result in enrich_jobs(limit=5):
        print(f"- {result['job_url']}")
        print(f"  domain: {result['domain']}")
        if result["error"]:
            print(f"  FAILED: {result['error']}")
        else:
            print(f"  hm_name: {result['hm_name']}")
            print(f"  hm_email: {result['hm_email']}")
        print()
