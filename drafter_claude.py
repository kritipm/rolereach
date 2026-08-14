import os

from dotenv import load_dotenv

import database
import telegram_bot

load_dotenv()

PORTFOLIO_URL = os.environ.get("PORTFOLIO_URL", "")
print(f"[drafter_claude] PORTFOLIO_URL loaded as: {PORTFOLIO_URL!r}")
if not PORTFOLIO_URL:
    print("[drafter_claude] WARNING: PORTFOLIO_URL is empty — emails will have a blank Portfolio line. "
          "Check it's set in .env locally, and in the workflow's env: block / GitHub secrets in CI.")

SUBJECT_LINE = "Built and Shipped. Applying for APM."

# Fixed, final template — no dynamic company-description generation anymore.
# Only {hm_name}, {job_title}, {company_name} are substituted per job.
EMAIL_BODY_TEMPLATE = """Hi {hm_name},

At Oho-Kids I worked on growth strategy for a creator ecosystem with 100K+ users — mapping activation drop-offs, running experiments, feeding insights back into the product. At Times Internet I worked across B2B and B2C products, owning analytics and driving product decisions on live surfaces.

Since then I've taken three products from problem to production independently — an AI-powered reachability tool, an onboarding activation funnel, and an automated pipeline running live in production every morning.

I came across the {job_title} opening at {company_name} and wanted to reach out directly. The live products, the decisions that shaped them, and the thinking behind each one are in the portfolio and CV attached.

{portfolio_url}

Happy to connect on a 15-minute call if there's a fit.
Kriti"""


def build_email(hm_name, job_title, company_name):
    first_name = (hm_name or "").split()[0] if hm_name else "there"
    body = EMAIL_BODY_TEMPLATE.format(
        hm_name=first_name,
        job_title=job_title or "this role",
        company_name=company_name or "your company",
        portfolio_url=PORTFOLIO_URL,
    )
    return f"Subject: {SUBJECT_LINE}\n\n{body}"


def draft_emails():
    jobs = database.fetch_jobs_needing_draft()
    results = []

    for job in jobs:
        company = job.get("author") or "your company"
        job_title = telegram_bot.get_title(job) or "this role"
        draft = build_email(job.get("hm_name"), job_title, company)
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
