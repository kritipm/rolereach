"""
Layer 2 — Fit Assessment

Role Fit is fully scored via five independent JD-evidence signals.
Experience / Skill / Portfolio / Domain Fit: structure scaffolded, scoring pending.
Overall Fit: structure scaffolded, weighting pending.

Every score is traceable to JD text. A signal is scored at its highest clearly
supported level — signals are not mutually exclusive.
"""

import json
import re

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _compile(patterns, flags=re.IGNORECASE | re.DOTALL):
    return [re.compile(p, flags) for p in patterns]


# If one of these verbs sits within 15 chars before a matched L3 action, the role
# is support-qualified ("help conduct", "assist in leading") — don't credit L3.
_SUPPORT_VERB_RE = re.compile(r"\b(help|assist|support)\b", re.IGNORECASE)


def _score_signal(text, l3_pats, l2_pats, l1_pats):
    """Return (score 0–3, evidence snippet) using the highest matching level."""
    for level, pats in ((3, l3_pats), (2, l2_pats), (1, l1_pats)):
        for pat in pats:
            m = pat.search(text)
            if m:
                if level == 3:
                    # Guard: don't claim L3 for "help conduct X" or "assist in leading Y"
                    prefix = text[max(0, m.start() - 15):m.start()]
                    if _SUPPORT_VERB_RE.search(prefix):
                        continue
                s = max(0, m.start() - 15)
                e = min(len(text), m.end() + 80)
                snippet = text[s:e].replace("\n", " ").strip()
                return level, snippet
    return 0, None


def _fit_level(score):
    """Map a 0–10 score to HIGH / MEDIUM / LOW."""
    if score >= 8.0:
        return "HIGH"
    if score >= 5.0:
        return "MEDIUM"
    return "LOW"


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 1 — Product Ownership
# ─────────────────────────────────────────────────────────────────────────────
# 3 = explicitly owns/leads a product, feature, roadmap, prioritization,
#     requirements, or product decision.
# 2 = drives/manages a defined part of product work, no clear end-to-end ownership.
# 1 = supports/contributes to product decisions, requirements, or feature work.

_OWN_L3 = _compile([
    r"\b(own|owns)\b.{0,60}(product[\s-]area|product|roadmap|backlog|prioriti|requirements?|prd|vision|strategy|feature)",
    r"\bend[\s-]to[\s-]end\b.{0,50}(ownership|product|feature|delivery|execution)",
    r"\b(responsible|accountable)\b.{0,40}(product|feature|roadmap|outcome|vision|strategy)",
    r"\b(define|set)\b.{0,40}product\s+(roadmap|vision|strategy|direction|priorit)",
    r"\b(lead|leads)\b.{0,40}(product\s+(?:area|roadmap|vision|strategy|initiative)|feature\s+area)",
    r"\bproduct\s+ownership\b",
    r"\b(create|write|define)\b.{0,30}(prd|product\s+requirements?\s+document|product\s+spec)",
    r"\b(own|drive|lead)\b.{0,30}product\s+(decision|direction|vision|goals?)",
    r"\bown\b.{0,30}(feature|product)",
])

_OWN_L2 = _compile([
    r"\b(drive|drives)\b.{0,60}(product|feature|roadmap|backlog|requirement|initiative)",
    r"\b(manage|manages)\b.{0,40}(product[\s-]backlog|feature|roadmap)",
    r"\b(write|create)\b.{0,30}(user\s+stor(?:y|ies)|specifications?|requirements?)",
    r"\bproduct\s+(requirements?|roadmap|backlog)\b",
    r"\b(gather|elicit)\b.{0,30}requirements?",
    r"\bprioritiz(?:e|ation|ing)\b",
    r"\buser\s+stor(?:y|ies)\b",
])

_OWN_L1 = _compile([
    r"\b(support|assist|help)\b.{0,60}(product|roadmap|feature|requirement|backlog)",
    r"\bcontribute\b.{0,40}(product|feature|roadmap|requirement)",
])


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 2 — Product Discovery
# ─────────────────────────────────────────────────────────────────────────────
# 3 = explicitly owns/conducts user research, problem/customer discovery,
#     or identifies user needs/problems.
# 2 = contributes to research, analysis, requirements gathering, or discovery.
# 1 = supports/assists research or discovery.

_DISC_L3 = _compile([
    r"\b(own|conduct|lead|run|drive)\b.{0,60}(user\s+research|customer\s+(?:research|discovery|interview))",
    r"\b(identify|discover|uncover)\b.{0,50}(user\s+(?:needs?|problems?|pain)|customer\s+(?:needs?|problems?|pain))",
    r"\b(problem|customer)\s+discovery\b",
    r"\bproblem\s+(?:definition|space|identification)\b",
    r"\b(understand|define)\b.{0,40}(user\s+needs?|customer\s+needs?|problem\s+space)",
    r"\b(own|lead|conduct)\b.{0,30}(interview|usability\s+test|user\s+test)",
    r"\bvoice\s+of\s+(?:the\s+)?customer\b",
    r"\b(work\s+with|talk\s+to)\b.{0,20}customers?.{0,40}(identify|understand|discover|uncover)",
])

_DISC_L2 = _compile([
    r"\b(user\s+research|customer\s+research|user\s+interviews?|customer\s+interviews?)\b",
    r"\b(gather|collect|analyze|analyse)\b.{0,40}(user\s+(?:feedback|insights?)|customer\s+(?:feedback|insights?))",
    r"\buser\s+(?:feedback|insights?|pain\s*points?)\b",
    r"\b(analyze|analyse|understand)\b.{0,40}(user\s+(?:behavior|behaviour|needs?)|customer\s+(?:behavior|behaviour|journey))",
    r"\brequirements?\s+gathering\b",
    r"\b(competitive\s+analysis|market\s+research)\b",
    r"\bidentify\b.{0,40}(problem|pain\s*point|challenge|need)\b",
])

_DISC_L1 = _compile([
    r"\b(support|assist|help)\b.{0,60}(research|discovery|interview)",
    r"\buser\s+testing\b",
    r"\b(usability|customer|user)\s+(test|study)\b",
])


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 3 — Product Execution
# ─────────────────────────────────────────────────────────────────────────────
# 3 = explicitly owns/drives product delivery, launches, or end-to-end execution.
# 2 = executes/co-delivers features or coordinates delivery.
# 1 = supports/assists product delivery or implementation.

_EXEC_L3 = _compile([
    r"\b(own|lead|drive)\b.{0,60}(product\s+(?:launch|delivery|release|shipping)|feature\s+(?:launch|delivery|shipping|release))",
    r"\bend[\s-]to[\s-]end\b.{0,40}(delivery|execution|launch|feature\s+delivery)",
    r"\b(own|lead|drive)\b.{0,40}(go[\s-]to[\s-]market|gtm)",
    r"\b(own|lead|manage)\b.{0,30}(sprint|scrum|agile\s+process|delivery\s+process)",
    r"\b(responsible|accountable)\b.{0,40}(launch|delivery|shipping|execution)",
])

_EXEC_L2 = _compile([
    r"\b(execute|deliver|ship|launch|release)\b.{0,60}(feature|product|sprint|milestone)",
    r"\b(coordinate)\b.{0,40}(engineering|design|development|delivery|launch)",
    r"\b(sprint\s+(?:planning|review|retrospective)|scrum|agile|kanban)\b",
    r"\bfeature\s+(?:delivery|launch|release|development)\b",
    r"\bgo[\s-]to[\s-]market\b",
    r"\b(work\s+with|collaborate\s+with)\b.{0,40}engineering.{0,50}(ship|build|deliver|launch)",
    r"\bproduct\s+(?:delivery|launch|release|shipping)\b",
    r"\b(ship|launch)\b.{0,30}(?:a\s+)?(?:new\s+)?(feature|product|update)\b",
])

_EXEC_L1 = _compile([
    r"\b(support|assist|help)\b.{0,60}(delivery|launch|execution|implementation|shipping)",
    r"\b(participate|contribute)\b.{0,30}(sprint|delivery|launch|execution)",
    r"\bimplementation\b",
])


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 4 — Product Metrics / Experimentation
# ─────────────────────────────────────────────────────────────────────────────
# 3 = owns/drives product metrics, experimentation, A/B tests, or product analytics.
# 2 = analyzes/uses product metrics or contributes to experiments.
# 1 = supports reporting, analytics, or measurement.

_METR_L3 = _compile([
    r"\b(own|define|drive|set)\b.{0,50}(product\s+metrics?|success\s+metrics?|north\s+star|kpi|okr)",
    r"\b(run|design|own|lead)\b.{0,40}(a[\s/]b\s+test(?:ing)?|experiment(?:ation)?)",
    r"\b(own|lead|drive)\b.{0,40}(product\s+analytics|measurement|product\s+(?:performance|outcome))",
    r"\bexperimentation\s+(?:platform|culture|framework|program)\b",
    r"\b(define|own|set)\b.{0,30}(success\s+(?:metrics?|criteria)|kpi|okr)",
])

_METR_L2 = _compile([
    r"\b(analyze|analyse|use|monitor|track)\b.{0,50}(metric|analytics?|kpi|okr|funnel|dashboard)",
    r"\ba[\s/]b\s+test(?:ing)?\b",
    r"\bdata[\s-](?:driven|informed)\b",
    r"\b(product\s+analytics?|mixpanel|amplitude|segment|heap|pendo|looker|tableau|google\s+analytics?)\b",
    r"\b(analyze|analyse)\b.{0,40}(user\s+(?:behavior|behaviour|data|engagement))",
    r"\bexperiment\b",
    r"\b(track|monitor)\b.{0,40}(metric|kpi|performance|engagement|retention|conversion|activation)",
])

_METR_L1 = _compile([
    r"\b(support|assist|help)\b.{0,60}(analytics|reporting|metrics?|measurement|data)",
    r"\b(report(?:ing)?|dashboard)\b.{0,20}(metric|kpi|analytic|performance)\b",
    r"\banalytics?\s+reporting\b",
])


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 5 — Cross-functional Product Work
# ─────────────────────────────────────────────────────────────────────────────
# 3 = explicitly drives/owns coordination across 2+ product-relevant functions.
# 2 = explicitly works/collaborates with another product-relevant function.
# 1 = general collaboration/teamwork mentioned but product-specific collaboration unclear.

_XFUNC_L3 = _compile([
    r"\b(drive|own|lead|manage)\b.{0,60}(cross[\s-]functional|stakeholder\s+alignment|cross[\s-]team\s+(?:coordination|collaboration|alignment))",
    r"\b(own|lead|drive|manage)\b.{0,50}alignment\b.{0,30}(across|between)\b.{0,60}(team|function|engineer|design|marketing)",
    r"\b(own|lead|drive)\b.{0,50}stakeholder\s+(?:management|communication|alignment)",
])

_XFUNC_L2 = _compile([
    r"\b(work\s+with|collaborate\s+with|partner\s+with|coordinate\s+with)\b.{0,60}(engineer(?:ing)?|design(?:er)?|ux|marketing|sales|data|operations?|legal|finance|customer\s+success)",
    r"\bcross[\s-]functional\b",
    r"\bstakeholder\s+(?:management|alignment|communication|engagement)\b",
    r"\b(interface|interact|engage)\s+with\b.{0,40}(team|function|stakeholder|engineer|design|marketing)",
    r"\bwork\s+closely\s+with\b.{0,40}(engineer|design|marketing|data|ops|product)",
])

_XFUNC_L1 = _compile([
    r"\b(team\s+player|teamwork|collaborative|work\s+well\s+with\b)",
    r"\b(collaborate|work\s+with)\b.{0,30}(team|colleague|member)\b",
    r"\bstakeholder\b",
])


# ─────────────────────────────────────────────────────────────────────────────
# Role Fit scoring
# ─────────────────────────────────────────────────────────────────────────────

def _score_role_fit(jd_text):
    own_s, own_ev = _score_signal(jd_text, _OWN_L3, _OWN_L2, _OWN_L1)
    disc_s, disc_ev = _score_signal(jd_text, _DISC_L3, _DISC_L2, _DISC_L1)
    exec_s, exec_ev = _score_signal(jd_text, _EXEC_L3, _EXEC_L2, _EXEC_L1)
    metr_s, metr_ev = _score_signal(jd_text, _METR_L3, _METR_L2, _METR_L1)
    xfnc_s, xfnc_ev = _score_signal(jd_text, _XFUNC_L3, _XFUNC_L2, _XFUNC_L1)

    total = own_s + disc_s + exec_s + metr_s + xfnc_s
    score = round((total / 15) * 10, 1)
    level = _fit_level(score)

    signals = {
        "product_ownership": {"score": own_s, "evidence": own_ev},
        "product_discovery": {"score": disc_s, "evidence": disc_ev},
        "product_execution": {"score": exec_s, "evidence": exec_ev},
        "product_metrics_experimentation": {"score": metr_s, "evidence": metr_ev},
        "cross_functional_product_work": {"score": xfnc_s, "evidence": xfnc_ev},
    }

    return score, level, total, signals


# ─────────────────────────────────────────────────────────────────────────────
# Pending dimensions — structure scaffolded, scoring rules not yet defined
# ─────────────────────────────────────────────────────────────────────────────

def _pending_dimension(name):
    return {"score": None, "level": "PENDING", "evidence": None}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def assess_fit(job):
    """
    Layer 2 — Fit Assessment.

    Input: job dict from DB (needs 'description' and 'text' fields).

    Returns a dict with:
        role_fit_score              float 0–10
        role_fit_level              HIGH | MEDIUM | LOW
        role_fit_signals            dict with per-signal score + evidence
        experience_fit_score        None (scoring pending)
        experience_fit_level        PENDING
        skill_fit_score             None (scoring pending)
        skill_fit_level             PENDING
        portfolio_fit_score         None (scoring pending)
        portfolio_fit_level         PENDING
        domain_fit_score            None (scoring pending)
        domain_fit_level            PENDING
        overall_fit_score           None (weighting rules pending)
        overall_fit_level           PENDING
        fit_evidence                JSON-serialisable dict of all evidence
    """
    description = (job.get("description") or "").strip()
    # Fall back to the full text field if no structured description was fetched
    jd_text = description or (job.get("text") or "")

    role_score, role_level, role_total, role_signals = _score_role_fit(jd_text)

    experience_fit = _pending_dimension("experience_fit")
    skill_fit = _pending_dimension("skill_fit")
    portfolio_fit = _pending_dimension("portfolio_fit")
    domain_fit = _pending_dimension("domain_fit")

    fit_evidence = {
        "role_fit": {
            "total_points": role_total,
            "max_points": 15,
            "signals": role_signals,
        },
        "experience_fit": experience_fit,
        "skill_fit": skill_fit,
        "portfolio_fit": portfolio_fit,
        "domain_fit": domain_fit,
    }

    return {
        "role_fit_score": role_score,
        "role_fit_level": role_level,
        "role_fit_signals": role_signals,
        "experience_fit_score": experience_fit["score"],
        "experience_fit_level": experience_fit["level"],
        "skill_fit_score": skill_fit["score"],
        "skill_fit_level": skill_fit["level"],
        "portfolio_fit_score": portfolio_fit["score"],
        "portfolio_fit_level": portfolio_fit["level"],
        "domain_fit_score": domain_fit["score"],
        "domain_fit_level": domain_fit["level"],
        "overall_fit_score": None,
        "overall_fit_level": "PENDING",
        "fit_evidence": json.dumps(fit_evidence, ensure_ascii=False),
    }
