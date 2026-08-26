import os

from dotenv import load_dotenv

import database

load_dotenv()

PORTFOLIO_URL = os.environ.get("PORTFOLIO_URL", "")
print(f"[drafter_claude] PORTFOLIO_URL loaded as: {PORTFOLIO_URL!r}")
if not PORTFOLIO_URL:
    print("[drafter_claude] WARNING: PORTFOLIO_URL is empty — emails will have a blank Portfolio line. "
          "Check it's set in .env locally, and in the workflow's env: block / GitHub secrets in CI.")

SUBJECT_LINE = "Diagnosed. Fixed. Shipped. Applying for APM."

# Fixed, final template — no dynamic company-description generation anymore.
# Only {hm_name}, {job_title}, {company_name} are substituted per job.
EMAIL_BODY_TEMPLATE = """Hi {hm_name},

I've been building things, not just talking about them.

At Times Internet I owned B2B and B2C funnels end to end. Diagnosed a drop-off no one had named, traced it to a value mismatch, rebuilt onboarding, got a 21% CTR lift. At Oho-Kids I ran growth strategy for a creator platform, flipped a core product assumption from ethnographic research, and built the foundation that drove 100K+ users and 48% first renewal retention by month 8. I wasn't around to see it land. The decisions held.

After that I kept going. Took three products through the full loop independently. Problem to hypothesis, hypothesis to build, build to ship, ship to measure, measure to iterate. All three are live. All decisions documented.

Came across the {job_title} at {company_name} and wanted to reach out directly.

Portfolio and CV say the rest better than I can here:
{portfolio_url}

Happy to get on a quick call if anything lands.
Kriti Kumari"""


# Inlined from telegram_bot.py rather than imported — importing that module pulls
# in a top-level os.environ["TELEGRAM_BOT_TOKEN"]/["TELEGRAM_CHAT_ID"] read that
# raises KeyError if unset, same issue fixed in dashboard.py in the last commit.
def get_title(job):
    if job["source"] == "hackernews":
        first_line = (job["text"] or "").split("\n", 1)[0]
        parts = [p.strip() for p in first_line.split("|")]
        return parts[1] if len(parts) > 1 else (job["matched_keyword"] or "N/A")

    return (job["text"] or "").split("|", 1)[0].strip() or "N/A"


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
        job_title = get_title(job) or "this role"
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
