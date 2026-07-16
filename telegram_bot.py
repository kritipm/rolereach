import argparse
import os

import requests
from dotenv import load_dotenv

import database
import experience_filter

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEND_MESSAGE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"


def get_title(job):
    if job["source"] == "hackernews":
        first_line = (job["text"] or "").split("\n", 1)[0]
        parts = [p.strip() for p in first_line.split("|")]
        return parts[1] if len(parts) > 1 else (job["matched_keyword"] or "N/A")

    return (job["text"] or "").split("|", 1)[0].strip() or "N/A"


def get_location(job):
    if job["source"] == "hackernews":
        first_line = (job["text"] or "").split("\n", 1)[0]
        parts = [p.strip() for p in first_line.split("|")]
        if len(parts) > 2 and parts[2]:
            return parts[2]
        return "Location not specified"

    parts = [p.strip() for p in (job["text"] or "").split("|")]
    if job["source"] in ("iimjobs", "internshala") and len(parts) >= 3 and parts[2]:
        return parts[2]
    if job["source"] == "google_jobs" and len(parts) >= 2 and parts[1]:
        return parts[1]

    return "Location not specified"


def get_experience(job):
    return job["experience_range"] or "Not specified"


def get_job_tier(job):
    """Tier 1 (0-1yr/fresher/unspecified) sorts before Tier 2 (1-2yr) in Telegram."""
    min_years = experience_filter.parse_min_experience(job["experience_range"] or "")
    combined_text = f"{get_title(job)} {job['experience_range'] or ''} {job.get('description') or ''}"
    return experience_filter.get_tier(min_years, combined_text) or 99


def strip_subject_line(draft):
    """email_draft already starts with 'Subject: ...' — the Telegram template hardcodes
    that line itself, so drop it from the draft body to avoid printing it twice."""
    if not draft:
        return draft

    lines = draft.split("\n")
    if lines and lines[0].strip().startswith("Subject:"):
        lines = lines[1:]
        if lines and lines[0].strip() == "":
            lines = lines[1:]

    return "\n".join(lines)


def format_message(job):
    header = [
        f"🆕 {get_title(job)} @ {job['author'] or 'N/A'}",
        f"📊 {get_experience(job)}",
        f"📍 {get_location(job)}",
    ]

    if job["hm_email"]:
        contact = [f"📧 {job['hm_email']} ({job['hm_name'] or 'N/A'})"]
        if job["company_linkedin"]:
            contact.append(f"💼 {job['company_linkedin']}")

        draft_body = strip_subject_line(job["email_draft"]) or ""
        draft_lines = [
            "📝 Draft:",
            "Subject: Built and Shipped. Applying for APM.",
            draft_body,
        ]

        sections = ["\n".join(header), "\n".join(contact), "\n".join(draft_lines), f"🔗 {job['url'] or 'N/A'}"]
    else:
        contact = ["⚠️ No email found"]
        if job["company_linkedin"]:
            contact.append(f"💼 {job['company_linkedin']}")

        sections = ["\n".join(header), "\n".join(contact), f"🔗 {job['url'] or 'N/A'}"]

    return "\n\n".join(sections)


def send_message(text):
    resp = requests.post(
        SEND_MESSAGE_URL,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def notify_all(source=None):
    jobs = database.fetch_unnotified_jobs(source=source)
    jobs.sort(key=get_job_tier)
    results = []

    for job in jobs:
        message = format_message(job)
        try:
            send_message(message)
        except requests.RequestException as exc:
            results.append({"job": job, "sent": False, "error": str(exc)})
            continue

        database.mark_notified(job["comment_id"])
        results.append({"job": job, "sent": True, "error": None})

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=None, help="Only send unnotified jobs from this source")
    args = parser.parse_args()

    database.init_db()
    results = notify_all(source=args.source)

    sent = sum(1 for r in results if r["sent"])
    failed = [r for r in results if not r["sent"]]

    scope = f" (source={args.source})" if args.source else ""
    print(f"Sent {sent}/{len(results)} messages{scope}.")
    for r in failed:
        print(f"FAILED: {r['job']['author']} ({r['job']['url']}) — {r['error']}")
