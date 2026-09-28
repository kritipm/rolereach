"""
Attack Route — Layer 4 of the RoleReach processing pipeline.

Answers three questions for each eligible opportunity:
  A. What attack routes are available?
  B. How strong is the available attack access?
  C. What attack priority and exact action plan should be generated?

Uses existing enrichment outputs only — does NOT rebuild contact discovery.
"""

import json

# Route type constants (displayed to the user in action sequences)
_ROUTE_APPLY = "Apply"
_ROUTE_PRODUCT_PERSON_LI = "Product Person LinkedIn"
_ROUTE_PRODUCT_FOLKS_LI = "Product Folks LinkedIn"
_ROUTE_EMAIL_E1 = "Email (attributed)"
_ROUTE_EMAIL_E2 = "Email (unattributed)"


# ---------------------------------------------------------------------------
# A. Route availability
# ---------------------------------------------------------------------------

def _parse_folks_linkedin(raw):
    """Parse product_folks_linkedin field — JSON list or bare URL — into a list."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(u).strip() for u in raw if u and str(u).strip()]
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(u).strip() for u in parsed if u and str(u).strip()]
        if isinstance(parsed, str) and parsed.strip():
            return [parsed.strip()]
    except (json.JSONDecodeError, TypeError):
        pass
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def _identify_routes(job):
    """
    Identify all available attack routes from existing job data.
    Returns a list of route dicts ordered strongest → weakest:
      1. Product Person LinkedIn
      2. Product Folks LinkedIn
      3. Email (attributed / E1)
      4. Email (unattributed / E2)
      5. Apply
    """
    product_person_li = (job.get("product_person_linkedin") or "").strip()
    product_person_name = (job.get("product_person_name") or "").strip()
    product_folks_raw = job.get("product_folks_linkedin")
    hm_name = (job.get("hm_name") or "").strip()
    hm_email = (job.get("hm_email") or "").strip()
    url = (job.get("url") or "").strip()

    routes = []

    if product_person_li:
        routes.append({
            "type": _ROUTE_PRODUCT_PERSON_LI,
            "linkedin_url": product_person_li,
            "name": product_person_name or hm_name or None,
        })

    folks_urls = _parse_folks_linkedin(product_folks_raw)
    if folks_urls:
        routes.append({
            "type": _ROUTE_PRODUCT_FOLKS_LI,
            "linkedin_urls": folks_urls,
        })

    if hm_email:
        if hm_name:
            routes.append({
                "type": _ROUTE_EMAIL_E1,
                "email": hm_email,
                "name": hm_name,
            })
        else:
            routes.append({
                "type": _ROUTE_EMAIL_E2,
                "email": hm_email,
            })

    if url:
        routes.append({"type": _ROUTE_APPLY, "url": url})

    return routes


# ---------------------------------------------------------------------------
# B. Attack access strength
# ---------------------------------------------------------------------------

def _attack_access(routes):
    """
    HIGH   — Product Person LinkedIn + attributed email (same person, both channels)
    MEDIUM — any single meaningful direct human route
    LOW    — application link or unattributed email only
    NONE   — nothing usable
    """
    types = {r["type"] for r in routes}

    if _ROUTE_PRODUCT_PERSON_LI in types and _ROUTE_EMAIL_E1 in types:
        return "HIGH"

    if any(t in types for t in (_ROUTE_PRODUCT_PERSON_LI, _ROUTE_PRODUCT_FOLKS_LI, _ROUTE_EMAIL_E1)):
        return "MEDIUM"

    if any(t in types for t in (_ROUTE_APPLY, _ROUTE_EMAIL_E2)):
        return "LOW"

    return "NONE"


# ---------------------------------------------------------------------------
# C. Attack priority
# ---------------------------------------------------------------------------

def _attack_priority(priority_level, access_level):
    """
    Returns (attack_priority, attack_intensity).

    HIGH priority → P1 DEEP regardless of access.
    MEDIUM priority → P2 STANDARD regardless of access.
    LOW priority + HIGH access → P2 STANDARD (upgraded).
    LOW priority + anything else → P3 LIGHT.
    """
    if priority_level == "HIGH":
        return "P1", "DEEP"
    if priority_level == "MEDIUM":
        return "P2", "STANDARD"
    if priority_level == "LOW":
        return ("P2", "STANDARD") if access_level == "HIGH" else ("P3", "LIGHT")
    # PENDING or unknown
    return "P3", "LIGHT"


# ---------------------------------------------------------------------------
# D. Action plan
# ---------------------------------------------------------------------------

def _primary_human_route(routes):
    """Return the strongest human-outreach route available, or None."""
    for r in routes:
        if r["type"] == _ROUTE_PRODUCT_PERSON_LI:
            return r
    for r in routes:
        if r["type"] == _ROUTE_PRODUCT_FOLKS_LI:
            return r
    for r in routes:
        if r["type"] == _ROUTE_EMAIL_E1:
            return r
    return None


def _sequence_label(route):
    """Human-readable action label for a route."""
    return {
        _ROUTE_PRODUCT_PERSON_LI: "LinkedIn (Product Person)",
        _ROUTE_PRODUCT_FOLKS_LI: "LinkedIn (Product Folks)",
        _ROUTE_EMAIL_E1: "Email",
        _ROUTE_EMAIL_E2: "Email (unattributed)",
        _ROUTE_APPLY: "Apply",
    }.get(route["type"], route["type"])


def _build_plan(attack_priority, routes, job):
    """
    Build the ordered action sequence, contact info, and URL outputs.

    P1 — DEEP:    strongest human routes first, then Apply
    P2 — STANDARD: one strong human route + Apply
    P3 — LIGHT:   Apply (or one human route if no Apply)
    """
    types = {r["type"] for r in routes}
    human = _primary_human_route(routes)
    has_apply = _ROUTE_APPLY in types
    has_e1 = _ROUTE_EMAIL_E1 in types
    has_ppl = _ROUTE_PRODUCT_PERSON_LI in types
    has_pfl = _ROUTE_PRODUCT_FOLKS_LI in types

    sequence = []

    if attack_priority == "P1":
        if has_ppl:
            sequence.append("LinkedIn (Product Person)")
            if has_e1:
                sequence.append("Email")
        elif has_pfl:
            sequence.append("LinkedIn (Product Folks)")
            if has_e1:
                sequence.append("Email")
        elif has_e1:
            sequence.append("Email")
        if has_apply:
            sequence.append("Apply")

    elif attack_priority == "P2":
        if human:
            sequence.append(_sequence_label(human))
            if has_apply:
                sequence.append("Apply")
        elif has_apply:
            sequence.append("Apply")

    else:  # P3
        if has_apply:
            sequence.append("Apply")
        elif human and human["type"] in (
            _ROUTE_PRODUCT_PERSON_LI, _ROUTE_PRODUCT_FOLKS_LI, _ROUTE_EMAIL_E1
        ):
            sequence.append(_sequence_label(human))

    if not sequence:
        sequence = ["No actionable routes available"]

    # Contact info
    primary_contact = None
    primary_contact_linkedin = None

    if human:
        if human["type"] == _ROUTE_PRODUCT_PERSON_LI:
            primary_contact = human.get("name") or None
            primary_contact_linkedin = human.get("linkedin_url")
        elif human["type"] == _ROUTE_PRODUCT_FOLKS_LI:
            urls = human.get("linkedin_urls", [])
            primary_contact_linkedin = urls[0] if urls else None
        elif human["type"] == _ROUTE_EMAIL_E1:
            primary_contact = human.get("name") or None

    hm_name = (job.get("hm_name") or "").strip()
    hm_email = (job.get("hm_email") or "").strip()
    url = (job.get("url") or "").strip()

    return {
        "primary_route": sequence[0],
        "first_action": sequence[0],
        "action_sequence": sequence,
        "primary_contact": primary_contact,
        "primary_contact_linkedin": primary_contact_linkedin,
        "attributed_email": hm_email if (hm_email and hm_name) else None,
        "job_application_url": url or None,
    }


def _build_reason(attack_priority, access_level, routes):
    """One-sentence reason for the chosen attack plan."""
    types = [r["type"] for r in routes]

    if attack_priority == "P1":
        base = "High-priority opportunity"
    elif attack_priority == "P2" and access_level == "HIGH":
        base = "Low-priority opportunity upgraded to STANDARD by HIGH attack access"
    elif attack_priority == "P2":
        base = "Medium-priority opportunity"
    else:
        base = "Low-priority opportunity"

    if _ROUTE_PRODUCT_PERSON_LI in types and _ROUTE_EMAIL_E1 in types:
        context = "direct access to a relevant product person via LinkedIn and verified email"
    elif _ROUTE_PRODUCT_PERSON_LI in types:
        context = "direct access to a relevant product person on LinkedIn"
    elif _ROUTE_PRODUCT_FOLKS_LI in types and _ROUTE_EMAIL_E1 in types:
        context = "product folks on LinkedIn and an attributed email"
    elif _ROUTE_PRODUCT_FOLKS_LI in types:
        context = "product folks at the company on LinkedIn"
    elif _ROUTE_EMAIL_E1 in types:
        context = "an attributed email contact"
    elif _ROUTE_EMAIL_E2 in types:
        context = "an unattributed email only"
    elif _ROUTE_APPLY in types:
        context = "an application link only"
    else:
        context = "no usable contact routes"

    return f"{base} with {context}."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def determine_attack_route(job):
    """
    Evaluate attack route for one job dict.

    Input: job dict from the DB (all fields, including priority_level from Layer 3).

    Output dict keys:
        attack_access_level         : HIGH | MEDIUM | LOW | NONE
        attack_priority             : P1 | P2 | P3
        attack_intensity            : DEEP | STANDARD | LIGHT
        attack_available_routes     : JSON list of route type strings
        attack_primary_route        : human-readable primary action
        attack_first_action         : same as primary_route
        attack_action_sequence      : JSON ordered list of action strings
        attack_primary_contact      : name of the primary contact (or None)
        attack_primary_contact_linkedin : LinkedIn URL (or None)
        attack_attributed_email     : attributed email address (or None)
        attack_job_application_url  : job/application URL (or None)
        attack_reason               : one-sentence explanation
    """
    routes = _identify_routes(job)
    access_level = _attack_access(routes)
    priority_level = (job.get("priority_level") or "PENDING").strip().upper()
    attack_priority, attack_intensity = _attack_priority(priority_level, access_level)
    plan = _build_plan(attack_priority, routes, job)
    reason = _build_reason(attack_priority, access_level, routes)

    return {
        "attack_access_level": access_level,
        "attack_priority": attack_priority,
        "attack_intensity": attack_intensity,
        "attack_available_routes": json.dumps([r["type"] for r in routes]),
        "attack_primary_route": plan["primary_route"],
        "attack_first_action": plan["first_action"],
        "attack_action_sequence": json.dumps(plan["action_sequence"]),
        "attack_primary_contact": plan["primary_contact"],
        "attack_primary_contact_linkedin": plan["primary_contact_linkedin"],
        "attack_attributed_email": plan["attributed_email"],
        "attack_job_application_url": plan["job_application_url"],
        "attack_reason": reason,
    }
