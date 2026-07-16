import os
import re

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import database

load_dotenv()

PORTFOLIO_URL = os.environ["PORTFOLIO_URL"]

EMAIL_TEMPLATE = """Subject: Built and Shipped. Applying for APM.

Hi {first_name},

I'm a PM with a UI/UX background — shipped live AI-integrated products independently, owned B2B and B2C funnels end to end across two internships.

I've been looking closely at {company}. {observation}

That's the kind of problem I work on. Find what's broken, build the fix, prove it moved.

Portfolio: {portfolio_url}
CV attached.
Happy to connect if there's a fit.
Kriti"""

FALLBACK_OBSERVATION = "[Could not automatically fetch a company description — add your own observation here]"

# Keyword -> specific friction point, checked in order; first match wins.
FRICTION_KEYWORD_MAP = [
    (["voice", "speech", "transcription", "call center", "contact center"],
     "getting consistent accuracy across accents and real-world audio conditions at scale"),
    (["crypto", "trading", "exchange"],
     "making a volatile, intimidating product feel safe enough for a first-time user to commit real money"),
    (["wage", "earned wage", "financial health", "lending", "credit", "fintech", "payments", "remittance", "banking"],
     "getting users to trust and actually adopt a new financial habit early on"),
    (["language learning", "edtech", "learning", "education", "e-learning"],
     "keeping learners engaged past the first week when initial motivation naturally drops"),
    (["menopause", "women's health", "care community", "healthcare", "wellness", "patient"],
     "getting people to open up about something they're used to staying quiet about"),
    (["endpoint", "saas", "daas", "vdi", "enterprise it", "it teams", "infrastructure"],
     "getting IT teams to trust a new layer of infra enough to roll it out beyond a pilot"),
    (["marketplace", "logistics", "supply chain", "delivery", "freight"],
     "keeping both sides of the marketplace engaged once the initial novelty wears off"),
    (["hiring", "recruit", "talent", "staffing", "jobs"],
     "getting both candidates and hiring managers to trust a faster process without feeling rushed"),
]
DEFAULT_FRICTION = "turning early interest into habitual, everyday usage"


def fetch_company_description(company_url):
    """Fetch the company's homepage and return raw candidate description text
    (meta description, og:description, or first substantial paragraph)."""
    if not company_url:
        return None

    try:
        resp = requests.get(company_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        return meta_desc["content"]

    og_desc = soup.find("meta", attrs={"property": "og:description"})
    if og_desc and og_desc.get("content"):
        return og_desc["content"]

    for p in soup.find_all("p"):
        text = p.get_text(strip=True)
        if len(text) > 40:
            return text

    return None


def summarize_what_they_do(company, text):
    """Reduce raw marketing copy to a short, plain noun phrase describing the product."""
    cleaned = text.strip()
    cleaned = re.sub(r"^(discover|join|welcome to)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^transform your [\w\s]+ with\s+", "", cleaned, flags=re.IGNORECASE)

    if company:
        # Homepages often refer to themselves by a shorter form than the full
        # scraped company name (e.g. "FINN App" on the page is just "FINN").
        if re.search(re.escape(company), cleaned, re.IGNORECASE):
            cleaned = re.sub(re.escape(company), "", cleaned, flags=re.IGNORECASE)
        else:
            first_word = company.split()[0] if company.split() else company
            cleaned = re.sub(rf"\b{re.escape(first_word)}\b", "", cleaned, count=1, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" ,.-")

    cleaned = re.sub(r"^(is|are|helps|provides|offers)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" ,.-")

    clause = re.split(r"[—.;:]", cleaned, maxsplit=1)[0].strip().strip(",")
    words = clause.split()
    if len(words) > 14:
        clause = " ".join(words[:14])

    return clause or None


def pick_friction(text):
    lowered = text.lower()
    for keywords, friction in FRICTION_KEYWORD_MAP:
        if any(keyword in lowered for keyword in keywords):
            return friction
    return DEFAULT_FRICTION


def build_observation(company, raw_description):
    if not raw_description:
        return FALLBACK_OBSERVATION

    what_they_do = summarize_what_they_do(company, raw_description)
    if not what_they_do:
        return FALLBACK_OBSERVATION

    friction = pick_friction(raw_description)
    return f"You're building {what_they_do} — the friction I'd focus on is {friction}."


def build_email(hm_name, company, observation):
    first_name = (hm_name or "").split()[0] if hm_name else "there"
    return EMAIL_TEMPLATE.format(
        first_name=first_name,
        company=company,
        observation=observation,
        portfolio_url=PORTFOLIO_URL,
    )


def draft_emails():
    jobs = database.fetch_jobs_needing_draft()
    results = []

    for job in jobs:
        company = job.get("author") or "your company"
        raw_description = fetch_company_description(job.get("company_url"))
        observation = build_observation(company, raw_description)
        draft = build_email(job.get("hm_name"), company, observation)
        database.update_email_draft(job["comment_id"], draft)
        results.append({"job": job, "draft": draft})

    return results


if __name__ == "__main__":
    database.init_db()
    for result in draft_emails():
        job = result["job"]
        print(f"=== {job['author']} — {job['hm_name']} <{job['hm_email']}> ===")
        print(result["draft"])
        print()
