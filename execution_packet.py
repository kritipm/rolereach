"""
Execution Packet — Layer 5 of the RoleReach processing pipeline.

Converts the Attack Plan (Layer 4) into ready-to-execute materials stored
in the DB:

  linkedin_draft    — personalized LinkedIn DM, voice-matched to the user's
                      established template
  execution_packet  — JSON blob of ordered steps consumed by the dashboard
"""

import json
import os

from dotenv import load_dotenv

load_dotenv()

PORTFOLIO_URL = os.environ.get("PORTFOLIO_URL", "https://kriti-portfolio-pm.vercel.app/")

# LinkedIn DM — matches the voice of the existing client-side dmTemplate exactly,
# so the server-side and client-side drafts are identical when personalized.
_LI_DM_NAMED = (
    "Hi {first_name}, came across the {job_title} opening at {company_name} and wanted to reach out "
    "directly. Two internships — one at a scaled product org, one at an early-stage startup — and "
    "three products shipped independently since, all live, all documented. Worth a look at the work: "
    "{portfolio_url} Would love your feedback on the work or any nudge in the right direction if "
    "there’s a fit. — Kriti"
)

_LI_DM_UNNAMED = (
    "Hi there, came across the {job_title} opening at {company_name} and wanted to reach out "
    "directly. Two internships — one at a scaled product org, one at an early-stage startup — and "
    "three products shipped independently since, all live, all documented. Worth a look at the work: "
    "{portfolio_url} Would love your feedback on the work or any nudge in the right direction if "
    "there’s a fit. — Kriti"
)


def _first_name(full_name):
    parts = (full_name or "").strip().split()
    return parts[0] if parts else None


def _job_title(job):
    return (job.get("text") or "").split("|", 1)[0].strip() or "this role"


def _company_name(job):
    return (job.get("author") or "").strip() or "your company"


def _build_linkedin_dm(contact_name, job_title, company_name):
    first = _first_name(contact_name)
    template = _LI_DM_NAMED if first else _LI_DM_UNNAMED
    return template.format(
        first_name=first or "",
        job_title=job_title or "this role",
        company_name=company_name or "your company",
        portfolio_url=PORTFOLIO_URL,
    )


def _parse_action_sequence(job):
    raw = job.get("attack_action_sequence") or "[]"
    try:
        seq = json.loads(raw)
        if isinstance(seq, list):
            return seq
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _split_email_draft(email_draft):
    """Return (subject, body) from the stored email_draft string."""
    if not email_draft:
        return None, None
    lines = email_draft.split("\n", 2)
    if lines and lines[0].startswith("Subject: "):
        subject = lines[0][len("Subject: "):].strip()
        body = lines[2].strip() if len(lines) > 2 else ""
        return subject, body
    return None, email_draft.strip()


def build_execution_packet(job):
    """
    Build the execution packet for one job dict.

    Returns:
        linkedin_draft    — LinkedIn DM text (None if no LinkedIn step in plan)
        execution_packet  — JSON string of ordered execution steps
    """
    action_sequence = _parse_action_sequence(job)
    title = _job_title(job)
    company = _company_name(job)

    contact_name = (
        job.get("attack_primary_contact")
        or job.get("product_person_name")
        or job.get("hm_name")
        or ""
    ).strip()

    contact_linkedin = (job.get("attack_primary_contact_linkedin") or "").strip() or None
    contact_role = (job.get("product_person_role") or "").strip() or None

    hm_name = (job.get("hm_name") or "").strip()
    hm_email = (
        job.get("attack_attributed_email") or job.get("hm_email") or ""
    ).strip() or None

    email_subject, email_body = _split_email_draft(job.get("email_draft"))

    apply_url = (
        job.get("attack_job_application_url") or job.get("url") or ""
    ).strip() or None

    linkedin_draft = None
    steps = []

    for i, action in enumerate(action_sequence, start=1):
        if action in ("LinkedIn (Product Person)", "LinkedIn (Product Folks)"):
            if linkedin_draft is None:
                linkedin_draft = _build_linkedin_dm(contact_name, title, company)
            steps.append({
                "step": i,
                "type": "linkedin",
                "label": action,
                "person_name": contact_name or None,
                "person_role": contact_role or None,
                "linkedin_url": contact_linkedin,
                "draft": linkedin_draft,
            })

        elif action == "Email":
            steps.append({
                "step": i,
                "type": "email",
                "label": "Email",
                "recipient_name": contact_name or hm_name or None,
                "recipient_email": hm_email,
                "subject": email_subject,
                "draft": email_body or None,
            })

        elif action == "Email (unattributed)":
            steps.append({
                "step": i,
                "type": "email_unattributed",
                "label": "Email (unattributed)",
                "recipient_email": (job.get("hm_email") or "").strip() or None,
                "note": "Unattributed email — apply directly or locate the hiring contact.",
            })

        elif action == "Apply":
            steps.append({
                "step": i,
                "type": "apply",
                "label": "Apply",
                "url": apply_url,
                "action_label": "OPEN",
                "portfolio_url": PORTFOLIO_URL or None,
            })

        else:
            steps.append({
                "step": i,
                "type": "none",
                "label": action,
            })

    if not steps:
        steps = [{"step": 1, "type": "none", "label": "No actionable routes available"}]

    packet = {
        "attack_priority": job.get("attack_priority"),
        "attack_intensity": job.get("attack_intensity"),
        "attack_access_level": job.get("attack_access_level"),
        "steps": steps,
    }

    return {
        "linkedin_draft": linkedin_draft,
        "execution_packet": json.dumps(packet, ensure_ascii=False),
    }
