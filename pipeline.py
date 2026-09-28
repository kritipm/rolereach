"""
Pipeline — Layer 6 of the RoleReach processing pipeline.

Pure functions: takes lists of user-marked events, returns pipeline state
and aggregated metrics. No DB calls. No scoring or prioritization.
"""

from datetime import datetime, timezone

EMAIL_SENT_WAIT_DAYS = 3
FOLLOWUP_SENT_WAIT_DAYS = 7

EVENT_TYPES = frozenset({
    "applied",
    "linkedin_sent",
    "email_sent",
    "followup_sent",
    "response",
    "conversation",
    "interview",
    "rejected",
    "offer",
})

EVENT_LABELS = {
    "applied": "Applied",
    "linkedin_sent": "LinkedIn Sent",
    "email_sent": "Email Sent",
    "followup_sent": "Follow-up Sent",
    "response": "Response",
    "conversation": "Conversation",
    "interview": "Interview",
    "rejected": "Rejected",
    "offer": "Offer",
}


def _parse_dt(s):
    if not s:
        return None
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def compute_pipeline_state(events, now=None):
    """
    Given a list of event dicts {event_type, noted_at} for one job,
    return a state dict.

    State precedence (highest wins):
      OFFER > INTERVIEW > CONVERSATION > RESPONDED > REJECTED >
      email-path states > WAITING (linkedin/apply) > NOT STARTED
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Collapse to first occurrence of each type
    by_type = {}
    for ev in events:
        et = ev.get("event_type")
        if not et or et in by_type:
            continue
        dt = _parse_dt(ev.get("noted_at"))
        if dt:
            by_type[et] = dt

    has = lambda t: t in by_type

    if has("offer"):
        state = "OFFER"
    elif has("interview"):
        state = "INTERVIEW"
    elif has("conversation"):
        state = "CONVERSATION"
    elif has("response"):
        state = "RESPONDED"
    elif has("rejected"):
        state = "REJECTED"
    elif has("email_sent"):
        if has("followup_sent"):
            days = (now - by_type["followup_sent"]).days
            state = "NO RESPONSE" if days >= FOLLOWUP_SENT_WAIT_DAYS else "WAITING"
        else:
            days = (now - by_type["email_sent"]).days
            state = "FOLLOW-UP DUE" if days >= EMAIL_SENT_WAIT_DAYS else "WAITING"
    elif has("linkedin_sent"):
        state = "WAITING"
    elif has("applied"):
        state = "APPLICATION SENT"
    else:
        state = "NOT STARTED"

    followup_due = (
        has("email_sent")
        and not has("followup_sent")
        and not has("response")
        and not has("rejected")
        and (now - by_type["email_sent"]).days >= EMAIL_SENT_WAIT_DAYS
    )

    return {
        "state": state,
        "followup_due": followup_due,
        "events_at": {et: dt.isoformat() for et, dt in by_type.items()},
        # Per-event booleans for easy dashboard rendering
        "applied": has("applied"),
        "linkedin_sent": has("linkedin_sent"),
        "email_sent": has("email_sent"),
        "followup_sent": has("followup_sent"),
        "response": has("response"),
        "conversation": has("conversation"),
        "interview": has("interview"),
        "rejected": has("rejected"),
        "offer": has("offer"),
    }


def compute_metrics(all_events_by_job):
    """
    all_events_by_job: dict {job_id: [event dicts]}

    Counts distinct jobs that have at least one event of each type.
    Rates are percentages, rounded to 1 decimal place (None if denominator is 0).
    """
    def count(event_type):
        return sum(
            1 for events in all_events_by_job.values()
            if any(e.get("event_type") == event_type for e in events)
        )

    def rate(num, denom):
        if not denom:
            return None
        return round((num / denom) * 100, 1)

    applications = count("applied")
    linkedin = count("linkedin_sent")
    emails = count("email_sent")
    followups = count("followup_sent")
    responses = count("response")
    conversations = count("conversation")
    interviews = count("interview")
    offers = count("offer")

    return {
        "applications_sent": applications,
        "linkedin_sent": linkedin,
        "emails_sent": emails,
        "followups_sent": followups,
        "responses": responses,
        "conversations": conversations,
        "interviews": interviews,
        "offers": offers,
        "application_to_response_rate": rate(responses, applications),
        "response_to_conversation_rate": rate(conversations, responses),
        "conversation_to_interview_rate": rate(interviews, conversations),
        "interview_to_offer_rate": rate(offers, interviews),
    }
