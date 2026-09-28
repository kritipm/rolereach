import os
from datetime import datetime, timedelta

from flask import Flask, jsonify, request

import config
import database
import experience_filter
import pipeline as pipeline_lib

app = Flask(__name__)

PIPELINE_PASSKEY = os.environ.get("PIPELINE_PASSKEY", "")


def _check_passkey():
    """Return True if the request carries the correct pipeline passkey."""
    if not PIPELINE_PASSKEY:
        return True  # No passkey configured → open (dev/local mode)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:] == PIPELINE_PASSKEY
    return False


def _require_passkey():
    """Return a 401 response if passkey check fails, else None."""
    if not _check_passkey():
        return jsonify({"error": "Unauthorized"}), 401
    return None


STATUS_CYCLE = ["NEW", "Sent", "Replied", "Interview", "Skip"]
SOURCES = ["hackernews", "cutshort", "iimjobs", "google_jobs", "internshala", "jsearch", "yc", "careers"]
SOURCE_LABELS = {
    "hackernews": "Hacker News",
    "cutshort": "Cutshort",
    "iimjobs": "iimjobs",
    "google_jobs": "Google Jobs",
    "internshala": "Internshala",
    "jsearch": "JSearch",
    "yc": "YC Jobs",
    "careers": "Direct Careers",
}
WEEKLY_GOAL_TARGET = 10


# ---------- Shared helpers ----------
# get_title/get_location/get_experience/get_job_tier are inlined from telegram_bot.py
# rather than imported — importing that module pulls in a top-level
# os.environ["TELEGRAM_BOT_TOKEN"]/["TELEGRAM_CHAT_ID"] read that raises KeyError if
# unset, which crashed the whole Flask app on startup on Render (render.yaml only
# declares DATABASE_URL, not the Telegram vars).


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
    """Tier 1 (0-1yr/fresher/unspecified) sorts before Tier 2 (1-2yr)."""
    min_years = experience_filter.parse_min_experience(job["experience_range"] or "")
    combined_text = f"{get_title(job)} {job['experience_range'] or ''} {job.get('description') or ''}"
    return experience_filter.get_tier(min_years, combined_text) or 99


def get_all_jobs_with_meta():
    database.init_db()
    with database.get_connection() as conn:
        jobs = [dict(r) for r in conn.execute("SELECT * FROM jobs").fetchall()]

    actions = database.fetch_all_job_actions()

    for job in jobs:
        job["title"] = get_title(job)
        job["location"] = get_location(job)
        job["experience"] = get_experience(job)
        job["tier"] = get_job_tier(job)
        job["has_email"] = bool(job.get("hm_email"))
        job["has_linkedin"] = bool(job.get("company_linkedin"))

        if job["has_email"]:
            job["group"] = "act_now"
        elif job["has_linkedin"]:
            job["group"] = "review"
        else:
            job["group"] = "no_contact"

        action = actions.get(job["comment_id"])
        job["status"] = action["status"] if action else "NEW"
        job["actioned_at"] = action["actioned_at"] if action else None

    return jobs


def last_run_timestamp():
    if config.DATABASE_URL:
        # No local file mtime to key off of when reading from Postgres.
        return None
    if not os.path.exists(config.DB_PATH):
        return None
    return datetime.fromtimestamp(os.path.getmtime(config.DB_PATH)).isoformat(timespec="seconds")


# ---------- API: Public aggregate stats (Layer 1-6 summary) ----------


@app.route("/api/summary")
def api_summary():
    """Public aggregate stats — no passkey required."""
    database.init_db()

    def _count(conn, query, params=()):
        r = conn.execute(query, params).fetchone()
        return (r["cnt"] or 0) if r else 0

    with database.get_connection() as conn:
        discovered = _count(conn, "SELECT COUNT(*) as cnt FROM jobs")
        eligible = _count(conn, "SELECT COUNT(*) as cnt FROM jobs WHERE eligibility_status = 'ELIGIBLE'")
        review = _count(conn, "SELECT COUNT(*) as cnt FROM jobs WHERE eligibility_status = 'REVIEW'")
        p1 = _count(conn, "SELECT COUNT(*) as cnt FROM jobs WHERE attack_priority='P1'")
        p2 = _count(conn, "SELECT COUNT(*) as cnt FROM jobs WHERE attack_priority='P2'")
        p3 = _count(conn, "SELECT COUNT(*) as cnt FROM jobs WHERE attack_priority='P3'")
        contacts = _count(
            conn,
            "SELECT COUNT(*) as cnt FROM jobs WHERE eligibility_status = 'ELIGIBLE'"
            " AND (hm_email IS NOT NULL OR company_linkedin IS NOT NULL)",
        )
        attacks_ready = _count(conn, "SELECT COUNT(*) as cnt FROM jobs WHERE execution_packet IS NOT NULL")
        today_cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        new_today = _count(
            conn,
            "SELECT COUNT(*) as cnt FROM jobs WHERE posted_at >= ? OR notified = 0",
            (today_cutoff,),
        )
        source_rows = conn.execute(
            "SELECT source, COUNT(*) as cnt FROM jobs"
            " WHERE eligibility_status = 'ELIGIBLE' GROUP BY source"
        ).fetchall()
        sources = {r["source"]: r["cnt"] for r in source_rows}

    return jsonify({
        "discovered": discovered,
        "eligible": eligible,
        "review": review,
        "p1": p1,
        "p2": p2,
        "p3": p3,
        "prioritized": p1 + p2 + p3,
        "contacts_found": contacts,
        "attacks_ready": attacks_ready,
        "new_today": new_today,
        "sources": sources,
        "last_run": last_run_timestamp(),
    })


# ---------- API: Enriched eligible opportunities (Layers 1–5) ----------


@app.route("/api/opportunities")
def api_opportunities():
    """Eligible jobs with all 6-layer data — no passkey required."""
    database.init_db()
    with database.get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs"
            " WHERE eligibility_status IN ('ELIGIBLE','REVIEW')"
            " AND attack_priority IS NOT NULL"
            " ORDER BY"
            " CASE attack_priority WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 ELSE 4 END,"
            " COALESCE(priority_score, 0) DESC"
        ).fetchall()

    jobs = []
    for r in rows:
        job = dict(r)
        jobs.append({
            "job_id": str(job["comment_id"]),
            "title": get_title(job),
            "company": job.get("author") or "",
            "source": job.get("source") or "",
            "source_label": SOURCE_LABELS.get(job.get("source") or "", job.get("source") or ""),
            "location": get_location(job),
            "experience": get_experience(job),
            "url": job.get("url"),
            "posted_at": job.get("posted_at"),
            "eligibility_status": job.get("eligibility_status"),
            "eligibility_reason": job.get("eligibility_reason"),
            "attack_priority": job.get("attack_priority"),
            "attack_intensity": job.get("attack_intensity"),
            "attack_reason": job.get("attack_reason"),
            "attack_action_sequence": job.get("attack_action_sequence"),
            "attack_access_level": job.get("attack_access_level"),
            "priority_score": job.get("priority_score"),
            "fit_score": job.get("fit_score"),
            "role_fit": job.get("role_fit"),
            "experience_fit": job.get("experience_fit"),
            "skill_fit": job.get("skill_fit"),
            "portfolio_fit": job.get("portfolio_fit"),
            "domain_fit": job.get("domain_fit"),
            "overall_fit": job.get("overall_fit"),
            "hm_email": job.get("hm_email"),
            "hm_name": job.get("hm_name"),
            "company_linkedin": job.get("company_linkedin"),
            "email_draft": job.get("email_draft"),
            "linkedin_draft": job.get("linkedin_draft"),
            "execution_packet": job.get("execution_packet"),
        })

    return jsonify(jobs)


# ---------- Pages ----------


@app.route("/")
def index():
    return DASHBOARD_HTML


# ---------- API: Agent tab ----------


@app.route("/api/agent")
def api_agent():
    jobs = get_all_jobs_with_meta()

    per_source_counts = {src: 0 for src in SOURCES}
    for job in jobs:
        if job["source"] in per_source_counts:
            per_source_counts[job["source"]] += 1

    tier1_count = sum(1 for j in jobs if j["tier"] == 1)
    tier2_count = sum(1 for j in jobs if j["tier"] == 2)
    named_email_count = sum(1 for j in jobs if j["has_email"])
    linkedin_only_count = sum(1 for j in jobs if not j["has_email"] and j["has_linkedin"])
    no_contact_count = sum(1 for j in jobs if not j["has_email"] and not j["has_linkedin"])
    drafts_ready = sum(1 for j in jobs if j.get("email_draft"))

    today_cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    new_today = 0
    with database.get_connection() as conn:
        result = conn.execute(
            "SELECT COUNT(*) as count FROM jobs WHERE posted_at >= ? OR notified = 0",
            (today_cutoff,)
        ).fetchone()
        if result:
            new_today = result["count"] if result["count"] else 0

    return jsonify(
        {
            "jobs_count": len(jobs),
            "new_today": new_today,
            "drafts_ready": drafts_ready,
            "last_run": last_run_timestamp(),
            "runs_daily_at": "8:00 AM",
            "tier1": {"count": tier1_count, "label": "Fresher / 0-1yr"},
            "tier2": {"count": tier2_count, "label": "1-3yr"},
            "sources": [
                {"name": SOURCE_LABELS[src], "count": per_source_counts[src]} for src in SOURCES
            ],
            "contact": {
                "named_email": named_email_count,
                "linkedin_only": linkedin_only_count,
                "no_contact": no_contact_count,
            },
        }
    )


# ---------- API: Actions tab ----------


@app.route("/api/jobs")
def api_jobs():
    status_filter = request.args.get("status", "All")
    jobs = get_all_jobs_with_meta()

    if status_filter != "All":
        jobs = [j for j in jobs if j["status"] == status_filter]

    jobs.sort(key=lambda j: (j["group"] != "act_now", j["group"] != "review", -(j["comment_id"] or 0)))

    return jsonify(
        [
            {
                "job_id": str(j["comment_id"]),
                "title": j["title"],
                "company": j["author"],
                "source": j["source"],
                "source_label": SOURCE_LABELS.get(j["source"], j["source"]),
                "experience": j["experience"],
                "tier": j["tier"],
                "location": j["location"],
                "hm_email": j.get("hm_email"),
                "hm_name": j.get("hm_name"),
                "company_linkedin": j.get("company_linkedin"),
                "email_draft": j.get("email_draft"),
                "url": j.get("url"),
                "status": j["status"],
                "group": j["group"],
                "posted_at": j.get("posted_at"),
                "attack_priority": j.get("attack_priority"),
                "attack_intensity": j.get("attack_intensity"),
                "attack_action_sequence": j.get("attack_action_sequence"),
                "execution_packet": j.get("execution_packet"),
                "linkedin_draft": j.get("linkedin_draft"),
            }
            for j in jobs
        ]
    )


@app.route("/api/action", methods=["POST"])
def api_action():
    payload = request.get_json(force=True, silent=True) or {}
    job_id = payload.get("job_id")
    if job_id is not None:
        try:
            job_id = int(job_id)
        except (ValueError, TypeError):
            pass
    status = payload.get("status")
    timestamp = payload.get("timestamp") or datetime.now().isoformat(timespec="seconds")

    if job_id is None or status not in STATUS_CYCLE:
        return jsonify({"error": f"status must be one of {STATUS_CYCLE}"}), 400

    database.set_job_action(job_id, status, timestamp)

    return jsonify({"job_id": job_id, "status": status, "timestamp": timestamp})


# ---------- API: Pipeline tab ----------


@app.route("/api/pipeline")
def api_pipeline():
    jobs = get_all_jobs_with_meta()
    actions = database.fetch_all_job_actions()

    seen = len(jobs)
    emailed = sum(1 for a in actions.values() if a["status"] in ("Sent", "Replied", "Interview"))
    replied = sum(1 for a in actions.values() if a["status"] in ("Replied", "Interview"))
    interview = sum(1 for a in actions.values() if a["status"] == "Interview")

    week_ago = datetime.now() - timedelta(days=7)
    weekly_sent = 0
    for a in actions.values():
        if a["status"] not in ("Sent", "Replied", "Interview"):
            continue
        try:
            actioned_at = datetime.fromisoformat(a["actioned_at"])
            if actioned_at.tzinfo is not None:
                actioned_at = actioned_at.replace(tzinfo=None)
        except (ValueError, TypeError):
            continue
        if actioned_at >= week_ago:
            weekly_sent += 1

    job_by_id = {j["comment_id"]: j for j in jobs}
    source_stats = {src: {"seen": 0, "sent": 0, "replies": 0} for src in SOURCES}
    for job in jobs:
        if job["source"] in source_stats:
            source_stats[job["source"]]["seen"] += 1

    for job_id, action in actions.items():
        job = job_by_id.get(job_id)
        if not job or job["source"] not in source_stats:
            continue
        if action["status"] in ("Sent", "Replied", "Interview"):
            source_stats[job["source"]]["sent"] += 1
        if action["status"] in ("Replied", "Interview"):
            source_stats[job["source"]]["replies"] += 1

    return jsonify(
        {
            "seen": seen,
            "emailed": emailed,
            "replied": replied,
            "interview": interview,
            "weekly_goal": {"current": weekly_sent, "target": WEEKLY_GOAL_TARGET},
            "sources": [
                {
                    "name": SOURCE_LABELS[src],
                    "seen": source_stats[src]["seen"],
                    "sent": source_stats[src]["sent"],
                    "replies": source_stats[src]["replies"],
                }
                for src in SOURCES
            ],
        }
    )


# ---------- API: Admin diagnostics & re-evaluation (passkey-gated) ----------


@app.route("/api/admin/diagnostics")
def api_admin_diagnostics():
    err = _require_passkey()
    if err:
        return err

    import eligibility as elig_module

    database.init_db()
    with database.get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) as cnt FROM jobs").fetchone()["cnt"]

        status_rows = conn.execute(
            "SELECT eligibility_status, COUNT(*) as cnt FROM jobs GROUP BY eligibility_status ORDER BY cnt DESC"
        ).fetchall()
        status_dist = {(r["eligibility_status"] or "NULL"): r["cnt"] for r in status_rows}

        reason_rows = conn.execute(
            "SELECT eligibility_reason, COUNT(*) as cnt FROM jobs "
            "WHERE eligibility_status = 'REJECT' GROUP BY eligibility_reason ORDER BY cnt DESC LIMIT 20"
        ).fetchall()
        top_reasons = [{"reason": r["eligibility_reason"], "count": r["cnt"]} for r in reason_rows]

        source_rows = conn.execute(
            "SELECT source, COUNT(*) as cnt FROM jobs GROUP BY source ORDER BY cnt DESC"
        ).fetchall()
        by_source = {r["source"]: r["cnt"] for r in source_rows}

        null_rows = conn.execute(
            "SELECT source, COUNT(*) as cnt FROM jobs WHERE eligibility_status IS NULL GROUP BY source"
        ).fetchall()
        null_by_source = {r["source"]: r["cnt"] for r in null_rows}

        # Sample: 5 NULL jobs with their computed (not yet stored) eligibility decision
        samples_raw = conn.execute(
            "SELECT * FROM jobs WHERE eligibility_status IS NULL LIMIT 5"
        ).fetchall()
        if not samples_raw:
            samples_raw = conn.execute(
                "SELECT * FROM jobs WHERE eligibility_status = 'REJECT' LIMIT 5"
            ).fetchall()

        samples = []
        for row in samples_raw:
            job = dict(row)
            result = elig_module.check_eligibility(job)
            text_preview = (job.get("text") or "")[:120].replace("\n", " ")
            samples.append({
                "comment_id": job["comment_id"],
                "source": job["source"],
                "text_preview": text_preview,
                "current_status": job.get("eligibility_status"),
                "computed_status": result["eligibility_status"],
                "computed_reason": result["eligibility_reason"],
                "role_category": result["role_category"],
                "seniority_status": result["seniority_status"],
                "experience_range": job.get("experience_range"),
            })

    return jsonify({
        "total": total,
        "status_distribution": status_dist,
        "by_source": by_source,
        "null_by_source": null_by_source,
        "top_rejection_reasons": top_reasons,
        "samples": samples,
    })


@app.route("/api/admin/run-eligibility", methods=["POST"])
def api_admin_run_eligibility():
    err = _require_passkey()
    if err:
        return err

    payload = request.get_json(force=True, silent=True) or {}
    rerun_all = bool(payload.get("rerun_all", False))

    import run_eligibility
    counts = run_eligibility.run(rerun_all=rerun_all)

    return jsonify({
        "status": "ok",
        "rerun_all": rerun_all,
        "counts": counts or {},
    })


@app.route("/api/admin/run-pipeline", methods=["POST"])
def api_admin_run_pipeline():
    err = _require_passkey()
    if err:
        return err

    import run_fit_assessment
    import run_priority
    import run_attack_route
    import run_execution_packet

    results = {}
    try:
        results["fit"] = run_fit_assessment.run() or {}
    except Exception as e:
        results["fit"] = {"error": str(e)}

    try:
        results["priority"] = run_priority.run() or {}
    except Exception as e:
        results["priority"] = {"error": str(e)}

    try:
        results["attack"] = run_attack_route.run() or {}
    except Exception as e:
        results["attack"] = {"error": str(e)}

    try:
        results["execution"] = run_execution_packet.run() or {}
    except Exception as e:
        results["execution"] = {"error": str(e)}

    return jsonify({"status": "ok", "results": results})


# ---------- API: DB sync (called by scheduler.py after each pipeline run) ----------


@app.route("/api/sync-db", methods=["POST"])
def api_sync_db():
    if config.DATABASE_URL:
        # Dashboard reads directly from Postgres now; no sqlite file to sync.
        return jsonify({"status": "skipped", "reason": "DATABASE_URL is set"})

    if not config.RAILWAY_TOKEN:
        return jsonify({"error": "RAILWAY_TOKEN is not configured on the server"}), 503

    auth_header = request.headers.get("Authorization", "")
    if auth_header != f"Bearer {config.RAILWAY_TOKEN}":
        return jsonify({"error": "unauthorized"}), 401

    uploaded = request.files.get("db")
    if uploaded is None:
        return jsonify({"error": "missing 'db' file in upload"}), 400

    tmp_path = config.DB_PATH + ".uploading"
    uploaded.save(tmp_path)
    os.replace(tmp_path, config.DB_PATH)

    return jsonify({"status": "ok", "bytes": os.path.getsize(config.DB_PATH)})


# ---------- API: Pipeline events (Layer 6 — passkey-gated) ----------


@app.route("/api/pipeline/log", methods=["POST"])
def api_pipeline_log():
    err = _require_passkey()
    if err:
        return err

    payload = request.get_json(force=True, silent=True) or {}
    job_id = payload.get("job_id")
    event_type = payload.get("event_type")
    action = payload.get("action", "set")
    noted_at = payload.get("noted_at") or datetime.now().isoformat(timespec="seconds")

    if not job_id:
        return jsonify({"error": "job_id required"}), 400
    if event_type not in pipeline_lib.EVENT_TYPES:
        return jsonify({"error": f"event_type must be one of {sorted(pipeline_lib.EVENT_TYPES)}"}), 400
    if action not in ("set", "unset"):
        return jsonify({"error": "action must be 'set' or 'unset'"}), 400

    try:
        job_id = int(job_id)
    except (ValueError, TypeError):
        return jsonify({"error": "job_id must be an integer"}), 400

    if action == "set":
        database.upsert_pipeline_event(job_id, event_type, noted_at)
    else:
        database.delete_pipeline_event(job_id, event_type)

    return jsonify({"ok": True, "job_id": job_id, "event_type": event_type, "action": action})


@app.route("/api/pipeline/state")
def api_pipeline_state():
    err = _require_passkey()
    if err:
        return err

    all_events = database.fetch_pipeline_events()
    jobs = get_all_jobs_with_meta()

    job_states = []
    for job in jobs:
        jid = job["comment_id"]
        events = all_events.get(jid, [])
        state = pipeline_lib.compute_pipeline_state(events)
        job_states.append({
            "job_id": str(jid),
            "title": job["title"],
            "company": job["author"],
            "location": job.get("location", ""),
            "attack_priority": job.get("attack_priority"),
            "attack_intensity": job.get("attack_intensity"),
            "posted_at": job.get("posted_at"),
            "url": job.get("url"),
            "hm_email": job.get("hm_email"),
            **state,
        })

    metrics = pipeline_lib.compute_metrics(all_events)
    return jsonify({"jobs": job_states, "metrics": metrics})


@app.route("/api/pipeline/metrics")
def api_pipeline_metrics():
    err = _require_passkey()
    if err:
        return err

    all_events = database.fetch_pipeline_events()
    metrics = pipeline_lib.compute_metrics(all_events)
    return jsonify(metrics)


# ---------- KPI: private dashboard (passkey-gated data, gate enforced on all API calls) ----------


@app.route("/kpi")
def kpi():
    return KPI_HTML


KPI_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RoleReach — Pipeline</title>
<style>
  :root {
    --bg: #09081C; --card: #100F2A; --card-hover: #171540;
    --border: #1A183C;
    --pink: #C830F0; --pink-glow: rgba(200,48,240,0.35);
    --pink-dark: #180828; --pink-border: #2C0A42;
    --purple: #8060C0; --lavender: #DDB0FF;
    --lavender-dark: #140C2C; --lavender-border: #201848;
    --green: #30E0A0; --green-dark: #051A10; --green-border: #0A3020;
    --yellow: #F0C040; --yellow-dark: #1A1000; --yellow-border: #302000;
    --red: #F04060; --red-dark: #1A0010; --red-border: #3A0020;
    --text-primary: #FFFFFF; --text-secondary: #EFEFEF;
    --text-muted: #B0B0B0; --text-dim: #505060;
    --gradient-hot: linear-gradient(135deg, #8060C0, #C830F0);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text-primary);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    min-height: 100vh; }

  /* ---------- passkey gate ---------- */
  #gate { display:flex; align-items:center; justify-content:center; min-height:100vh; }
  .gate-card { background:var(--card); border:1px solid var(--border); border-radius:16px;
    padding:40px 36px; max-width:380px; width:100%; text-align:center; }
  .gate-title { font-size:22px; font-weight:800; margin-bottom:6px; }
  .gate-sub { font-size:13px; color:var(--text-muted); margin-bottom:28px; }
  .gate-input { width:100%; background:var(--bg); border:1px solid var(--border);
    color:var(--text-primary); font-size:15px; padding:12px 16px; border-radius:10px;
    outline:none; margin-bottom:14px; }
  .gate-input:focus { border-color:var(--pink); }
  .gate-btn { width:100%; background:var(--gradient-hot); color:#fff; border:none;
    font-size:14px; font-weight:800; padding:13px; border-radius:10px; cursor:pointer; }
  .gate-btn:hover { opacity:0.9; }
  .gate-error { font-size:12px; color:var(--red); margin-top:10px; }

  /* ---------- main layout ---------- */
  #app { display:none; }
  header { display:flex; align-items:center; justify-content:space-between;
    padding:18px 24px; border-bottom:1px solid var(--border);
    position:sticky; top:0; background:var(--bg); z-index:10; }
  .header-title { font-size:18px; font-weight:800; background:var(--gradient-hot);
    -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; }
  .header-sub { font-size:12px; color:var(--text-muted); margin-left:10px; }
  .logout-btn { background:none; border:1px solid var(--border); color:var(--text-muted);
    font-size:12px; padding:6px 14px; border-radius:999px; cursor:pointer; }
  .logout-btn:hover { border-color:var(--pink); color:var(--pink); }

  .main { max-width:1060px; margin:0 auto; padding:24px 20px 60px; }

  /* ---------- metrics grid ---------- */
  .metrics-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(130px, 1fr));
    gap:10px; margin-bottom:24px; }
  .metric-card { background:var(--card); border:1px solid var(--border); border-radius:12px;
    padding:16px 14px; }
  .metric-val { font-size:28px; font-weight:900; line-height:1; }
  .metric-label { font-size:11px; color:var(--text-muted); font-weight:700;
    text-transform:uppercase; letter-spacing:0.5px; margin-top:5px; }
  .metric-card.pink .metric-val { color:var(--pink); }
  .metric-card.lav .metric-val { color:var(--lavender); }
  .metric-card.green .metric-val { color:var(--green); }
  .metric-card.yellow .metric-val { color:var(--yellow); }

  /* ---------- funnel ---------- */
  .section-title { font-size:13px; font-weight:800; color:var(--text-muted);
    text-transform:uppercase; letter-spacing:0.6px; margin-bottom:12px; }
  .funnel-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(200px, 1fr));
    gap:10px; margin-bottom:28px; }
  .funnel-card { background:var(--card); border:1px solid var(--border); border-radius:12px;
    padding:16px 14px; }
  .funnel-rate { font-size:26px; font-weight:900; color:var(--lavender); }
  .funnel-label { font-size:11.5px; color:var(--text-muted); margin-top:4px; }
  .funnel-null { font-size:18px; font-weight:700; color:var(--text-dim); }

  /* ---------- attention ---------- */
  .attention-block { background:var(--red-dark); border:1px solid var(--red-border);
    border-radius:12px; padding:16px 18px; margin-bottom:24px; }
  .attention-title { font-size:13px; font-weight:800; color:var(--red);
    text-transform:uppercase; letter-spacing:0.5px; margin-bottom:12px; }
  .attention-row { display:flex; align-items:center; gap:12px; padding:8px 0;
    border-bottom:1px solid rgba(240,64,96,0.15); }
  .attention-row:last-child { border-bottom:none; }
  .attention-company { font-size:13px; font-weight:700; color:var(--text-primary); min-width:140px; }
  .attention-title-text { font-size:12px; color:var(--text-muted); flex:1; }

  /* ---------- pipeline list ---------- */
  .pipeline-search { width:100%; background:var(--card); border:1px solid var(--border);
    color:var(--text-primary); font-size:13px; padding:10px 14px; border-radius:10px;
    outline:none; margin-bottom:14px; }
  .pipeline-search:focus { border-color:var(--pink); }
  .pipeline-job { background:var(--card); border:1px solid var(--border); border-radius:12px;
    padding:14px 16px; margin-bottom:8px; }
  .pipeline-job-top { display:flex; align-items:flex-start; gap:10px; margin-bottom:8px; flex-wrap:wrap; }
  .pipeline-job-title { font-size:14px; font-weight:800; flex:1; min-width:160px; }
  .pipeline-job-company { font-size:12px; color:var(--text-muted); }
  .state-badge { display:inline-flex; align-items:center; padding:3px 10px;
    border-radius:999px; font-size:10px; font-weight:800; letter-spacing:0.5px; white-space:nowrap; }
  .state-OFFER { background:#1A1400; border:1px solid #504000; color:#F0C040; }
  .state-INTERVIEW { background:var(--pink-dark); border:1px solid var(--pink-border); color:var(--pink); box-shadow:0 0 8px var(--pink-glow); }
  .state-CONVERSATION { background:var(--green-dark); border:1px solid var(--green-border); color:var(--green); }
  .state-RESPONDED { background:var(--green-dark); border:1px solid var(--green-border); color:var(--green); }
  .state-REJECTED { background:var(--red-dark); border:1px solid var(--red-border); color:var(--red); }
  .state-FOLLOW-UP\ DUE { background:var(--red-dark); border:1px solid var(--red-border); color:var(--red); animation:pulse-red 2s infinite; }
  @keyframes pulse-red { 0%,100%{box-shadow:0 0 0 0 rgba(240,64,96,0)} 50%{box-shadow:0 0 0 4px rgba(240,64,96,0.25)} }
  .state-NO\ RESPONSE { background:rgba(60,20,30,0.5); border:1px solid var(--red-border); color:#A04060; }
  .state-WAITING { background:var(--yellow-dark); border:1px solid var(--yellow-border); color:var(--yellow); }
  .state-APPLICATION\ SENT { background:var(--lavender-dark); border:1px solid var(--lavender-border); color:var(--lavender); }
  .state-NOT\ STARTED { background:transparent; border:1px solid var(--border); color:var(--text-dim); }

  .attack-chip { display:inline-flex; align-items:center; padding:2px 8px;
    border-radius:999px; font-size:10px; font-weight:800; white-space:nowrap; }
  .attack-chip.p1 { background:var(--pink-dark); border:1px solid var(--pink-border); color:var(--pink); }
  .attack-chip.p2 { background:var(--lavender-dark); border:1px solid var(--lavender-border); color:var(--lavender); }
  .attack-chip.p3 { background:rgba(80,80,96,0.18); border:1px solid var(--border); color:var(--text-muted); }

  /* ---------- event buttons ---------- */
  .event-btns { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
  .event-btn { font-size:11px; font-weight:700; padding:5px 12px; border-radius:999px;
    border:1px solid var(--border); background:transparent; color:var(--text-muted);
    cursor:pointer; transition:all 0.15s; white-space:nowrap; }
  .event-btn:hover { border-color:var(--pink); color:var(--pink); }
  .event-btn.active { border-color:var(--green); color:var(--green); background:var(--green-dark); }
  .event-btn.active.negative { border-color:var(--red); color:var(--red); background:var(--red-dark); }
  .event-btn.active.gold { border-color:#F0C040; color:#F0C040; background:#1A1400; }
  .event-btn.due { border-color:var(--red); color:var(--red); animation:pulse-red 2s infinite; }
  .event-noted { font-size:10px; color:var(--text-dim); margin-top:4px; }

  /* ---------- loading / empty ---------- */
  .loading { text-align:center; padding:60px 20px; color:var(--text-dim); font-size:14px; }
  .empty-note { text-align:center; padding:40px 20px; color:var(--text-dim); font-size:13px; }
</style>
</head>
<body>

<!-- Passkey gate -->
<div id="gate">
  <div class="gate-card">
    <div class="gate-title">RoleReach</div>
    <div class="gate-sub">Private pipeline &amp; KPI dashboard</div>
    <input id="pk-input" class="gate-input" type="password" placeholder="Enter passkey"
      onkeydown="if(event.key==='Enter') unlock()">
    <button class="gate-btn" onclick="unlock()">Unlock</button>
    <div id="gate-error" class="gate-error"></div>
  </div>
</div>

<!-- Main app (shown after auth) -->
<div id="app">
  <header>
    <div style="display:flex; align-items:baseline; gap:8px;">
      <span class="header-title">RoleReach</span>
      <span class="header-sub">Pipeline &amp; KPI</span>
    </div>
    <button class="logout-btn" onclick="logout()">Lock</button>
  </header>

  <div class="main">
    <!-- Metrics -->
    <div class="section-title" style="margin-bottom:12px;">Outreach</div>
    <div id="metrics-grid" class="metrics-grid"><div class="loading">Loading…</div></div>

    <!-- Funnel -->
    <div class="section-title">Conversion Funnel</div>
    <div id="funnel-grid" class="funnel-grid"></div>

    <!-- Attention: follow-ups due -->
    <div id="attention-block"></div>

    <!-- Pipeline -->
    <div class="section-title" style="margin-top:4px;">Pipeline</div>
    <input id="pipeline-search" class="pipeline-search" type="text" placeholder="Filter by title or company…"
      oninput="renderPipeline()">
    <div id="pipeline-list"></div>
  </div>
</div>

<script>
const EVENT_LABELS = {
  applied: "Applied",
  linkedin_sent: "LinkedIn Sent",
  email_sent: "Email Sent",
  followup_sent: "Follow-up Sent",
  response: "Response",
  conversation: "Conversation",
  interview: "Interview",
  rejected: "Rejected",
  offer: "Offer",
};

const EVENT_ORDER = ["applied", "linkedin_sent", "email_sent", "followup_sent", "response", "conversation", "interview", "rejected", "offer"];

const NEGATIVE_EVENTS = new Set(["rejected"]);
const GOLD_EVENTS = new Set(["offer", "interview"]);

let cachedData = null;
let passkey = "";

function escHtml(s) {
  if (!s) return "";
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function formatDate(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleDateString("en-IN", { day:"numeric", month:"short" });
  } catch(e) { return ""; }
}

// ---------- Auth ----------

function unlock() {
  const val = document.getElementById("pk-input").value.trim();
  if (!val) { document.getElementById("gate-error").textContent = "Enter a passkey."; return; }
  passkey = val;
  loadData(true);
}

function logout() {
  passkey = "";
  try { sessionStorage.removeItem("rr_pk"); } catch(e) {}
  document.getElementById("app").style.display = "none";
  document.getElementById("gate").style.display = "flex";
  document.getElementById("pk-input").value = "";
  cachedData = null;
}

// ---------- Data fetch ----------

async function loadData(fromUnlock = false) {
  try {
    const res = await fetch("/api/pipeline/state", {
      headers: { "Authorization": "Bearer " + passkey }
    });
    if (res.status === 401) {
      if (fromUnlock) {
        document.getElementById("gate-error").textContent = "Wrong passkey. Try again.";
      }
      return;
    }
    const data = await res.json();
    cachedData = data;
    try { sessionStorage.setItem("rr_pk", passkey); } catch(e) {}
    document.getElementById("gate").style.display = "none";
    document.getElementById("app").style.display = "block";
    renderMetrics(data.metrics);
    renderFunnel(data.metrics);
    renderAttention(data.jobs);
    renderPipeline();
  } catch(e) {
    if (fromUnlock) document.getElementById("gate-error").textContent = "Failed to connect.";
  }
}

// ---------- Metrics ----------

function renderMetrics(m) {
  const cards = [
    { val: m.applications_sent, label: "Applications", cls: "lav" },
    { val: m.linkedin_sent,     label: "LinkedIn Sent", cls: "lav" },
    { val: m.emails_sent,       label: "Emails Sent",   cls: "pink" },
    { val: m.followups_sent,    label: "Follow-ups",    cls: "pink" },
    { val: m.responses,         label: "Responses",     cls: "green" },
    { val: m.conversations,     label: "Conversations", cls: "green" },
    { val: m.interviews,        label: "Interviews",    cls: "yellow" },
    { val: m.offers,            label: "Offers",        cls: "yellow" },
  ];
  document.getElementById("metrics-grid").innerHTML = cards.map(c =>
    `<div class="metric-card ${c.cls}">
      <div class="metric-val">${c.val}</div>
      <div class="metric-label">${c.label}</div>
    </div>`
  ).join("");
}

// ---------- Funnel ----------

function renderFunnel(m) {
  const rows = [
    { rate: m.application_to_response_rate,   label: "Application → Response" },
    { rate: m.response_to_conversation_rate,  label: "Response → Conversation" },
    { rate: m.conversation_to_interview_rate, label: "Conversation → Interview" },
    { rate: m.interview_to_offer_rate,        label: "Interview → Offer" },
  ];
  document.getElementById("funnel-grid").innerHTML = rows.map(r =>
    `<div class="funnel-card">
      ${r.rate !== null
        ? `<div class="funnel-rate">${r.rate}%</div>`
        : `<div class="funnel-null">&mdash;</div>`}
      <div class="funnel-label">${r.label}</div>
    </div>`
  ).join("");
}

// ---------- Attention ----------

function renderAttention(jobs) {
  const due = jobs.filter(j => j.followup_due);
  if (!due.length) {
    document.getElementById("attention-block").innerHTML = "";
    return;
  }
  const rows = due.map(j =>
    `<div class="attention-row">
      <span class="attention-company">${escHtml(j.company)}</span>
      <span class="attention-title-text">${escHtml(j.title)}</span>
      <button class="event-btn due" onclick="logEvent('${j.job_id}', 'followup_sent', this)">Mark Follow-up Sent</button>
    </div>`
  ).join("");
  document.getElementById("attention-block").innerHTML =
    `<div class="attention-block">
      <div class="attention-title">⚠️ Follow-up Due (${due.length})</div>
      ${rows}
    </div>`;
}

// ---------- Pipeline ----------

const STATE_PRIORITY = {
  "OFFER":1,"INTERVIEW":2,"CONVERSATION":3,"RESPONDED":4,
  "FOLLOW-UP DUE":5,"WAITING":6,"APPLICATION SENT":7,
  "REJECTED":8,"NO RESPONSE":9,"NOT STARTED":10,
};

function renderPipeline() {
  if (!cachedData) return;
  const q = (document.getElementById("pipeline-search").value || "").toLowerCase();
  const jobs = cachedData.jobs
    .filter(j => !q || j.title.toLowerCase().includes(q) || j.company.toLowerCase().includes(q))
    .sort((a, b) => {
      const pa = STATE_PRIORITY[a.state] || 99;
      const pb = STATE_PRIORITY[b.state] || 99;
      if (pa !== pb) return pa - pb;
      const ap = {P1:1,P2:2,P3:3}[a.attack_priority] || 9;
      const bp = {P1:1,P2:2,P3:3}[b.attack_priority] || 9;
      return ap - bp;
    });

  if (!jobs.length) {
    document.getElementById("pipeline-list").innerHTML = '<div class="empty-note">No jobs match.</div>';
    return;
  }

  document.getElementById("pipeline-list").innerHTML = jobs.map(jobCard).join("");
}

function jobCard(j) {
  const stateClass = "state-" + j.state;
  const apClass = j.attack_priority ? j.attack_priority.toLowerCase() : "";
  const apLabel = j.attack_priority
    ? `${j.attack_priority}${j.attack_intensity ? " — " + j.attack_intensity : ""}`
    : "";

  const eventBtns = EVENT_ORDER.map(et => {
    const active = j[et];
    const negCls = NEGATIVE_EVENTS.has(et) && active ? " negative" : "";
    const goldCls = GOLD_EVENTS.has(et) && active ? " gold" : "";
    const dueCls = et === "followup_sent" && j.followup_due && !active ? " due" : "";
    const notedAt = active && j.events_at && j.events_at[et] ? formatDate(j.events_at[et]) : "";
    return `<button class="event-btn${active ? " active" + negCls + goldCls : dueCls}"
      onclick="logEvent('${j.job_id}', '${et}', this)"
      title="${active ? "Logged: " + notedAt + " — click to remove" : "Mark as done"}"
    >${escHtml(EVENT_LABELS[et])}${active && notedAt ? " · " + notedAt : ""}</button>`;
  }).join("");

  return `<div class="pipeline-job" id="pjob-${j.job_id}">
    <div class="pipeline-job-top">
      <div style="flex:1; min-width:200px;">
        <div class="pipeline-job-title">${escHtml(j.title)}</div>
        <div class="pipeline-job-company">${escHtml(j.company)}${j.location ? " · " + escHtml(j.location) : ""}</div>
      </div>
      <div style="display:flex; flex-direction:column; align-items:flex-end; gap:6px;">
        <span class="state-badge ${stateClass}">${escHtml(j.state)}</span>
        ${apLabel ? `<span class="attack-chip ${apClass}">${escHtml(apLabel)}</span>` : ""}
      </div>
    </div>
    <div class="event-btns">${eventBtns}</div>
  </div>`;
}

// ---------- Event logging ----------

async function logEvent(jobId, eventType, btn) {
  if (!cachedData) return;
  const job = cachedData.jobs.find(j => j.job_id === String(jobId));
  if (!job) return;

  const isActive = job[eventType];
  const action = isActive ? "unset" : "set";

  if (isActive && !confirm(`Remove "${EVENT_LABELS[eventType]}" from this job?`)) return;

  btn.disabled = true;
  try {
    const res = await fetch("/api/pipeline/log", {
      method: "POST",
      headers: { "Authorization": "Bearer " + passkey, "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: jobId,
        event_type: eventType,
        action: action,
        noted_at: new Date().toISOString(),
      }),
    });
    if (res.status === 401) { logout(); return; }
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert("Error: " + (err.error || res.status));
      return;
    }
    await loadData();
  } catch(e) {
    alert("Network error.");
  } finally {
    btn.disabled = false;
  }
}

// ---------- Boot ----------

(function() {
  let saved = "";
  try { saved = sessionStorage.getItem("rr_pk") || ""; } catch(e) {}
  if (saved) {
    passkey = saved;
    loadData();
  }
})();
</script>
</body>
</html>
"""


DASHBOARD_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RoleReach</title>
<style>
:root {
  --bg: #09081C; --card: #100F2A; --card-hover: #171540; --border: #1A183C;
  --pink: #C830F0; --pink-glow: rgba(200,48,240,0.35);
  --pink-dark: #180828; --pink-border: #2C0A42;
  --purple: #8060C0; --lavender: #DDB0FF;
  --lavender-dark: #140C2C; --lavender-border: #201848;
  --green: #30E0A0; --green-dark: #051A10; --green-border: #0A3020;
  --yellow: #F0C040; --yellow-dark: #1A1000; --yellow-border: #302000;
  --red: #F04060; --red-dark: #1A0010; --red-border: #3A0020;
  --text-primary: #FFFFFF; --text-secondary: #EFEFEF;
  --text-muted: #B0B0B0; --text-dim: #505060;
  --gradient-hot: linear-gradient(135deg, #8060C0, #C830F0);
  --p1: #C830F0; --p1-dark: #180828; --p1-border: #2C0A42; --p1-glow: rgba(200,48,240,0.35);
  --p2: #8060C0; --p2-dark: #140C2C; --p2-border: #201848;
  --p3: #606070; --p3-dark: rgba(80,80,96,0.15); --p3-border: #3A3A4C;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text-primary);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;min-height:100vh;}
.mono{font-family:"SF Mono","Cascadia Code",Consolas,monospace;}

/* ---- NAV ---- */
.site-header{display:flex;align-items:center;justify-content:space-between;padding:0 28px;height:52px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg);z-index:100;}
.logo{font-size:17px;font-weight:800;background:var(--gradient-hot);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;}
.tab-nav{display:flex;gap:2px;}
.tab-btn{background:none;border:none;color:var(--text-dim);font-size:12px;font-weight:800;padding:8px 14px;cursor:pointer;border-bottom:2px solid transparent;letter-spacing:0.5px;transition:color 0.15s,border-color 0.15s;}
.tab-btn:hover{color:var(--text-muted);}
.tab-btn.active{color:var(--pink);border-bottom-color:var(--pink);}
.tab-content{display:none;}
.tab-content.active{display:block;}

/* ---- HOME ---- */
.home-wrap{max-width:900px;margin:0 auto;padding:40px 24px 60px;}
.home-hero{margin-bottom:36px;}
.home-hero h1{font-size:32px;font-weight:800;line-height:1.2;background:linear-gradient(90deg,#FFF,var(--pink));-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;}
.home-hero p{font-size:14px;color:var(--text-muted);margin-top:8px;max-width:480px;line-height:1.6;}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-bottom:40px;}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:18px 16px;}
.stat-val{font-size:32px;font-weight:900;line-height:1;}
.stat-label{font-size:11px;color:var(--text-muted);font-weight:700;text-transform:uppercase;letter-spacing:0.5px;margin-top:5px;}
.stat-card.pink .stat-val{color:var(--pink);}
.stat-card.lav .stat-val{color:var(--lavender);}
.stat-card.green .stat-val{color:var(--green);}
.stat-card.dim .stat-val{color:var(--text-primary);}

.journey-section{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:24px;}
.journey-label{font-size:11px;font-weight:800;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.6px;margin-bottom:16px;}
.journey-stages{display:flex;flex-direction:column;gap:10px;}
.journey-stage{display:flex;align-items:center;gap:14px;padding:10px 14px;border-radius:10px;background:var(--bg);}
.stage-num{width:26px;height:26px;border-radius:50%;background:var(--gradient-hot);display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:800;flex-shrink:0;}
.stage-name{font-size:13px;font-weight:800;color:var(--text-primary);min-width:90px;}
.stage-desc{font-size:12px;color:var(--text-muted);}
.today-bar{display:flex;align-items:center;gap:10px;margin-top:18px;padding:12px 16px;background:var(--pink-dark);border:1px solid var(--pink-border);border-radius:10px;font-size:13px;}
.today-dot{width:8px;height:8px;border-radius:50%;background:var(--pink);box-shadow:0 0 8px var(--pink-glow);}

/* ---- AGENT ---- */
.agent-filter{display:flex;gap:8px;padding:14px 20px;border-bottom:1px solid var(--border);}
.p-pill{background:var(--card);border:1px solid var(--border);color:var(--text-dim);padding:5px 14px;border-radius:999px;font-size:12px;font-weight:800;cursor:pointer;letter-spacing:0.4px;transition:all 0.15s;}
.p-pill:hover{color:var(--text-muted);}
.p-pill.p1.active,.p-pill.p1:hover{background:var(--p1-dark);border-color:var(--p1-border);color:var(--p1);box-shadow:0 0 10px var(--p1-glow);}
.p-pill.p2.active,.p-pill.p2:hover{background:var(--p2-dark);border-color:var(--p2-border);color:var(--p2);}
.p-pill.p3.active,.p-pill.p3:hover{background:var(--p3-dark);border-color:var(--p3-border);color:var(--p3);}
.p-pill.all.active{background:rgba(200,48,240,0.1);border-color:var(--pink);color:var(--pink);}
.agent-layout{display:grid;grid-template-columns:320px 1fr;height:calc(100vh - 104px);overflow:hidden;}
@media(max-width:768px){.agent-layout{grid-template-columns:1fr;height:auto;overflow:visible;}}
.agent-list-col{overflow-y:auto;border-right:1px solid var(--border);}
.agent-detail-col{overflow-y:auto;padding:24px;}
.opp-card{padding:14px 16px;border-bottom:1px solid var(--border);cursor:pointer;transition:background 0.1s;}
.opp-card:hover{background:var(--card);}
.opp-card.selected{background:var(--card);border-left:2px solid var(--pink);}
.opp-title{font-size:13px;font-weight:700;color:var(--text-primary);margin-bottom:3px;}
.opp-company{font-size:12px;color:var(--text-muted);}
.opp-meta{display:flex;align-items:center;gap:6px;margin-top:6px;}
.p-chip{display:inline-flex;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:800;letter-spacing:0.4px;}
.p-chip.p1{background:var(--p1-dark);border:1px solid var(--p1-border);color:var(--p1);}
.p-chip.p2{background:var(--p2-dark);border:1px solid var(--p2-border);color:var(--p2);}
.p-chip.p3{background:var(--p3-dark);border:1px solid var(--p3-border);color:var(--p3);}
.fit-num{font-size:11px;color:var(--text-dim);font-weight:700;}

.detail-empty{display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-dim);font-size:13px;}
.detail-header{margin-bottom:20px;}
.detail-priority{margin-bottom:10px;}
.detail-title{font-size:20px;font-weight:800;margin-bottom:4px;}
.detail-company{font-size:14px;color:var(--text-muted);margin-bottom:10px;}
.detail-section{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:14px;}
.detail-section-title{font-size:11px;font-weight:800;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px;}
.why-text{font-size:13px;color:var(--text-secondary);line-height:1.6;}
.fit-row{display:flex;align-items:center;gap:10px;margin-bottom:8px;}
.fit-label{font-size:12px;color:var(--text-muted);width:120px;flex-shrink:0;}
.fit-track{flex:1;height:5px;background:rgba(255,255,255,0.06);border-radius:3px;overflow:hidden;}
.fit-fill{height:100%;border-radius:3px;background:var(--gradient-hot);}
.fit-score{font-size:12px;font-weight:700;color:var(--lavender);width:24px;text-align:right;flex-shrink:0;}
.action-seq{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;}
.seq-step{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:700;color:var(--text-secondary);}
.seq-num{width:20px;height:20px;border-radius:50%;background:var(--p1-dark);border:1px solid var(--p1-border);color:var(--p1);font-size:10px;font-weight:800;display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.seq-arrow{color:var(--text-dim);font-size:12px;}

/* ---- EXECUTION STEPS (shared) ---- */
.exec-step{border:1px solid var(--border);border-radius:10px;padding:12px 14px;margin-bottom:8px;background:var(--bg);}
.exec-step-hdr{display:flex;align-items:center;gap:10px;margin-bottom:8px;}
.exec-step-num{width:22px;height:22px;border-radius:50%;background:var(--p1-dark);border:1px solid var(--p1-border);color:var(--p1);font-size:11px;font-weight:800;display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.exec-step-lbl{font-size:11px;font-weight:800;color:var(--text-secondary);text-transform:uppercase;letter-spacing:0.5px;}
.exec-person{font-size:13px;font-weight:700;color:var(--text-primary);margin-bottom:4px;}
.exec-email{font-size:12px;color:var(--text-muted);margin-bottom:4px;font-family:"SF Mono",Consolas,monospace;}
.exec-subject{font-size:11.5px;color:var(--pink);margin-bottom:6px;}
.draft-box{background:var(--bg);border-radius:6px;padding:10px 12px;font-size:12px;line-height:1.55;white-space:pre-wrap;color:var(--lavender);max-height:200px;overflow-y:auto;margin-bottom:8px;}
.exec-btns{display:flex;gap:6px;flex-wrap:wrap;}
.btn-sm{display:inline-flex;align-items:center;justify-content:center;gap:5px;background:var(--card);border:1px solid var(--border);color:var(--text-muted);border-radius:8px;padding:6px 12px;font-size:12px;font-weight:700;cursor:pointer;transition:border-color 0.15s,color 0.15s;text-decoration:none;font-family:inherit;}
.btn-sm:hover{border-color:var(--pink);color:var(--pink);}
.btn-sm.primary{border-color:var(--purple);color:var(--lavender);}
.btn-sm.primary:hover{border-color:var(--pink);color:var(--pink);}

/* ---- ACTIONS ---- */
.actions-wrap{max-width:900px;margin:0 auto;padding:24px 20px 60px;}
.group-block{margin-bottom:32px;}
.group-title{font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:0.6px;margin-bottom:12px;display:flex;align-items:center;gap:10px;}
.group-title.followup{color:var(--red);}
.group-title.response{color:var(--green);}
.group-title.inprogress{color:var(--yellow);}
.group-title.newactions{color:var(--pink);}
.group-count{font-size:11px;padding:2px 8px;border-radius:999px;font-weight:800;}
.group-count.followup{background:var(--red-dark);border:1px solid var(--red-border);color:var(--red);}
.group-count.response{background:var(--green-dark);border:1px solid var(--green-border);color:var(--green);}
.group-count.inprogress{background:var(--yellow-dark);border:1px solid var(--yellow-border);color:var(--yellow);}
.group-count.newactions{background:var(--p1-dark);border:1px solid var(--p1-border);color:var(--p1);}

.action-card{background:var(--card);border:1px solid var(--border);border-radius:12px;margin-bottom:10px;overflow:hidden;}
.action-card-hdr{display:flex;align-items:center;gap:12px;padding:14px 16px;cursor:pointer;}
.action-card-hdr:hover{background:var(--card-hover);}
.action-card-body{display:none;border-top:1px solid var(--border);padding:16px;}
.action-card.open .action-card-body{display:block;}
.action-title-wrap{flex:1;min-width:0;}
.action-title{font-size:14px;font-weight:700;color:var(--text-primary);}
.action-company{font-size:12px;color:var(--text-muted);}
.action-toggle{color:var(--text-dim);font-size:12px;flex-shrink:0;transition:transform 0.15s;}
.action-card.open .action-toggle{transform:rotate(180deg);}

.pk-inline{background:var(--lavender-dark);border:1px solid var(--lavender-border);border-radius:10px;padding:14px;margin-top:12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
.pk-inline input{background:var(--bg);border:1px solid var(--border);color:var(--text-primary);padding:7px 12px;border-radius:8px;font-size:13px;outline:none;flex:1;min-width:160px;}
.pk-inline input:focus{border-color:var(--pink);}
.pk-inline-label{font-size:12px;color:var(--lavender);font-weight:700;}

/* ---- PIPELINE ---- */
.pipeline-wrap{max-width:900px;margin:0 auto;padding:24px 20px 60px;}
.funnel-track{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-bottom:32px;}
.funnel-card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:18px 16px;}
.funnel-val{font-size:34px;font-weight:900;line-height:1;color:var(--lavender);}
.funnel-label{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted);margin-top:6px;}
.funnel-rate{font-size:12px;color:var(--text-dim);margin-top:3px;}
.funnel-arrow{color:var(--text-dim);font-size:22px;align-self:center;display:none;}
@media(min-width:600px){.funnel-track{grid-template-columns:1fr auto 1fr auto 1fr auto 1fr;}.funnel-arrow{display:block;}}

.section-hdr{font-size:12px;font-weight:800;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:12px;}
.source-table{width:100%;border-collapse:collapse;}
.source-table th{text-align:left;font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.4px;padding:8px 12px;border-bottom:1px solid var(--border);}
.source-table td{padding:10px 12px;border-bottom:1px solid var(--border);font-size:13px;}
.source-table td.name{color:var(--text-primary);font-weight:700;}
.source-table td.num{font-family:"SF Mono",Consolas,monospace;color:var(--text-secondary);}
.src-panel{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:24px;}

/* ---- KPI tab inline gate ---- */
.kpi-gate-wrap{display:flex;align-items:center;justify-content:center;padding:80px 20px;}
.kpi-gate-card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:36px;max-width:360px;width:100%;text-align:center;}
.kpi-gate-title{font-size:20px;font-weight:800;margin-bottom:6px;}
.kpi-gate-sub{font-size:13px;color:var(--text-muted);margin-bottom:24px;}
.kpi-gate-input{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text-primary);font-size:15px;padding:11px 14px;border-radius:10px;outline:none;margin-bottom:12px;}
.kpi-gate-input:focus{border-color:var(--pink);}
.kpi-gate-btn{width:100%;background:var(--gradient-hot);color:#fff;border:none;font-size:14px;font-weight:800;padding:12px;border-radius:10px;cursor:pointer;}
.kpi-gate-btn:hover{opacity:0.9;}
.kpi-gate-err{font-size:12px;color:var(--red);margin-top:8px;}
.kpi-wrap{max-width:960px;margin:0 auto;padding:24px 20px 60px;}
.kpi-logout{float:right;background:none;border:1px solid var(--border);color:var(--text-muted);font-size:12px;padding:5px 12px;border-radius:999px;cursor:pointer;margin-bottom:16px;}
.kpi-logout:hover{border-color:var(--pink);color:var(--pink);}
.metrics-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px;margin-bottom:24px;}
.metric-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px 14px;}
.metric-val{font-size:28px;font-weight:900;line-height:1;}
.metric-lbl{font-size:11px;color:var(--text-muted);font-weight:700;text-transform:uppercase;letter-spacing:0.5px;margin-top:5px;}
.metric-card.pink .metric-val{color:var(--pink);}
.metric-card.lav .metric-val{color:var(--lavender);}
.metric-card.green .metric-val{color:var(--green);}
.metric-card.yellow .metric-val{color:var(--yellow);}
.kpi-section-title{font-size:12px;font-weight:800;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:12px;}
.conv-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;margin-bottom:28px;}
.conv-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px;}
.conv-rate{font-size:24px;font-weight:900;color:var(--lavender);}
.conv-lbl{font-size:11.5px;color:var(--text-muted);margin-top:4px;}
.conv-null{font-size:18px;font-weight:700;color:var(--text-dim);}
.att-block{background:var(--red-dark);border:1px solid var(--red-border);border-radius:12px;padding:14px 16px;margin-bottom:20px;}
.att-title{font-size:12px;font-weight:800;color:var(--red);text-transform:uppercase;letter-spacing:0.4px;margin-bottom:10px;}
.att-row{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid rgba(240,64,96,0.1);}
.att-row:last-child{border-bottom:none;}
.att-company{font-size:13px;font-weight:700;min-width:130px;}
.att-title-text{font-size:12px;color:var(--text-muted);flex:1;}
.pipe-search{width:100%;background:var(--card);border:1px solid var(--border);color:var(--text-primary);font-size:13px;padding:9px 13px;border-radius:10px;outline:none;margin-bottom:12px;}
.pipe-search:focus{border-color:var(--pink);}
.pipe-job{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:13px 15px;margin-bottom:8px;}
.pipe-job-top{display:flex;align-items:flex-start;gap:10px;margin-bottom:8px;flex-wrap:wrap;}
.pipe-job-title{font-size:14px;font-weight:800;flex:1;min-width:160px;}
.pipe-job-co{font-size:12px;color:var(--text-muted);}
.state-badge{display:inline-flex;padding:3px 10px;border-radius:999px;font-size:10px;font-weight:800;letter-spacing:0.4px;white-space:nowrap;}
.s-OFFER{background:#1A1400;border:1px solid #504000;color:#F0C040;}
.s-INTERVIEW{background:var(--p1-dark);border:1px solid var(--p1-border);color:var(--p1);box-shadow:0 0 8px var(--p1-glow);}
.s-CONVERSATION,.s-RESPONDED{background:var(--green-dark);border:1px solid var(--green-border);color:var(--green);}
.s-REJECTED{background:var(--red-dark);border:1px solid var(--red-border);color:var(--red);}
.s-FOLLOW-UP\ DUE{background:var(--red-dark);border:1px solid var(--red-border);color:var(--red);animation:pulseRed 2s infinite;}
@keyframes pulseRed{0%,100%{box-shadow:0 0 0 0 rgba(240,64,96,0)}50%{box-shadow:0 0 0 4px rgba(240,64,96,0.2)}}
.s-NO\ RESPONSE{background:rgba(60,20,30,0.5);border:1px solid var(--red-border);color:#A04060;}
.s-WAITING{background:var(--yellow-dark);border:1px solid var(--yellow-border);color:var(--yellow);}
.s-APPLICATION\ SENT{background:var(--lavender-dark);border:1px solid var(--lavender-border);color:var(--lavender);}
.s-NOT\ STARTED{background:transparent;border:1px solid var(--border);color:var(--text-dim);}
.event-btns{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;}
.event-btn{font-size:11px;font-weight:700;padding:5px 11px;border-radius:999px;border:1px solid var(--border);background:transparent;color:var(--text-muted);cursor:pointer;transition:all 0.15s;white-space:nowrap;}
.event-btn:hover{border-color:var(--pink);color:var(--pink);}
.event-btn.active{border-color:var(--green);color:var(--green);background:var(--green-dark);}
.event-btn.active.neg{border-color:var(--red);color:var(--red);background:var(--red-dark);}
.event-btn.active.gold{border-color:#F0C040;color:#F0C040;background:#1A1400;}
.event-btn.due{border-color:var(--red);color:var(--red);animation:pulseRed 2s infinite;}

.loading{text-align:center;padding:50px 20px;color:var(--text-dim);font-size:13px;}
.empty{text-align:center;padding:30px 20px;color:var(--text-dim);font-size:13px;}
</style>
</head>
<body>

<header class="site-header">
  <div class="logo">RoleReach</div>
  <nav class="tab-nav">
    <button class="tab-btn active" data-tab="home">HOME</button>
    <button class="tab-btn" data-tab="agent">AGENT</button>
    <button class="tab-btn" data-tab="actions">ACTIONS</button>
    <button class="tab-btn" data-tab="pipeline">PIPELINE</button>
    <button class="tab-btn" data-tab="kpi">KPIs &#128274;</button>
  </nav>
</header>

<!-- HOME -->
<section id="tab-home" class="tab-content active">
  <div class="home-wrap">
    <div class="home-hero">
      <h1>RoleReach is working.</h1>
      <p>Your personal PM hiring agent — discovering, scoring, and attacking opportunities around the clock.</p>
    </div>
    <div id="home-stats" class="stat-grid"><div class="loading">Loading…</div></div>
    <div class="journey-section">
      <div class="journey-label">HOW IT WORKS — 7-STAGE PIPELINE</div>
      <div class="journey-stages">
        <div class="journey-stage"><div class="stage-num">1</div><div class="stage-name">DISCOVER</div><div class="stage-desc">Scan 8 job boards every morning: Cutshort, Google Jobs, iimjobs, Internshala, YC, HN, JSearch, Direct Careers</div></div>
        <div class="journey-stage"><div class="stage-num">2</div><div class="stage-name">SCREEN</div><div class="stage-desc">Eligibility gate: experience range, title match, role type — ELIGIBLE / REVIEW / REJECT</div></div>
        <div class="journey-stage"><div class="stage-num">3</div><div class="stage-name">SCORE</div><div class="stage-desc">Fit Assessment across 5 dimensions: role · experience · skill · portfolio · domain (0–10 each)</div></div>
        <div class="journey-stage"><div class="stage-num">4</div><div class="stage-name">RANK</div><div class="stage-desc">Priority Engine: Fit × 0.6 + Freshness × 0.25 + Contact Access × 0.15 → P1 / P2 / P3</div></div>
        <div class="journey-stage"><div class="stage-num">5</div><div class="stage-name">PLAN</div><div class="stage-desc">Attack Route: P1 DEEP (LinkedIn + Email + Apply) · P2 STANDARD · P3 LIGHT</div></div>
        <div class="journey-stage"><div class="stage-num">6</div><div class="stage-name">EXECUTE</div><div class="stage-desc">Execution Packet: LinkedIn DM + personalised email draft generated in your voice, ready to copy</div></div>
        <div class="journey-stage"><div class="stage-num">7</div><div class="stage-name">PIPELINE</div><div class="stage-desc">Follow-up tracking: Applied → Sent → Follow-up Due → Response → Conversation → Interview → Offer</div></div>
      </div>
      <div id="home-today" class="today-bar"><div class="today-dot"></div><span id="home-today-text" style="font-size:13px;color:var(--text-secondary);">Loading today's update…</span></div>
    </div>
  </div>
</section>

<!-- AGENT -->
<section id="tab-agent" class="tab-content">
  <div class="agent-filter" id="agent-filter">
    <button class="p-pill all active" data-p="ALL">ALL</button>
    <button class="p-pill p1" data-p="P1">P1 DEEP</button>
    <button class="p-pill p2" data-p="P2">P2 STANDARD</button>
    <button class="p-pill p3" data-p="P3">P3 LIGHT</button>
  </div>
  <div class="agent-layout">
    <div class="agent-list-col" id="agent-list"><div class="loading">Loading opportunities…</div></div>
    <div class="agent-detail-col" id="agent-detail"><div class="detail-empty">Select a job to see the full attack plan.</div></div>
  </div>
</section>

<!-- ACTIONS -->
<section id="tab-actions" class="tab-content">
  <div class="actions-wrap" id="actions-container"><div class="loading">Loading…</div></div>
</section>

<!-- PIPELINE -->
<section id="tab-pipeline" class="tab-content">
  <div class="pipeline-wrap">
    <div class="section-hdr">Discovery Funnel</div>
    <div id="pipeline-funnel" class="funnel-track"><div class="loading">Loading…</div></div>
    <div class="src-panel">
      <div class="section-hdr">Eligible Jobs by Source</div>
      <table class="source-table">
        <thead><tr><th>Source</th><th>Eligible</th></tr></thead>
        <tbody id="pipeline-sources"></tbody>
      </table>
    </div>
  </div>
</section>

<!-- KPIs -->
<section id="tab-kpi" class="tab-content">
  <div id="kpi-gate-wrap" class="kpi-gate-wrap">
    <div class="kpi-gate-card">
      <div class="kpi-gate-title">Private Dashboard</div>
      <div class="kpi-gate-sub">Pipeline, KPIs &amp; outreach data</div>
      <input id="kpi-pk" class="kpi-gate-input" type="password" placeholder="Enter passkey"
        onkeydown="if(event.key==='Enter')kpiUnlock()">
      <button class="kpi-gate-btn" onclick="kpiUnlock()">Unlock</button>
      <div id="kpi-err" class="kpi-gate-err"></div>
    </div>
  </div>
  <div id="kpi-content" style="display:none;">
    <div class="kpi-wrap">
      <button class="kpi-logout" onclick="kpiLock()">Lock</button>
      <div class="kpi-section-title" style="margin-bottom:12px;">Outreach</div>
      <div id="kpi-metrics" class="metrics-grid"></div>
      <div class="kpi-section-title">Conversion Funnel</div>
      <div id="kpi-conv" class="conv-grid"></div>
      <div id="kpi-att"></div>
      <div class="kpi-section-title" style="margin-top:4px;">Pipeline</div>
      <input id="kpi-search" class="pipe-search" type="text" placeholder="Filter by title or company…" oninput="renderKpiList()">
      <div id="kpi-list"></div>
    </div>
  </div>
</section>

<script>
// ---- State ----
let summaryData = null;
let oppsData = null;
let selectedJobId = null;
let agentFilter = 'ALL';
let kpiPasskey = '';
let kpiData = null;
let actionsLoaded = false;

// ---- Utilities ----
function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function fmtDate(iso) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleDateString('en-IN',{day:'numeric',month:'short'}); } catch(e){return '';}
}
function getSavedPk() { try{return sessionStorage.getItem('rr_pk')||'';}catch(e){return '';} }
function savePk(pk) { try{sessionStorage.setItem('rr_pk',pk);}catch(e){} }
function copyText(text, btn, label) {
  const orig = btn.textContent;
  const doIt = () => { btn.textContent = 'Copied!'; btn.style.color='var(--green)'; setTimeout(()=>{btn.textContent=label||orig;btn.style.color='';},1800); };
  navigator.clipboard.writeText(text).then(doIt).catch(()=>{
    const ta=document.createElement('textarea'); ta.value=text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta); doIt();
  });
}

// ---- Tab switching ----
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));
  document.querySelectorAll('.tab-content').forEach(s=>s.classList.toggle('active',s.id==='tab-'+name));
  if (name==='home') loadHome();
  if (name==='agent') loadAgent();
  if (name==='actions') loadActions();
  if (name==='pipeline') loadPipeline();
  if (name==='kpi') initKpi();
}
document.querySelectorAll('.tab-btn').forEach(b=>b.addEventListener('click',()=>switchTab(b.dataset.tab)));

// ---- HOME ----
async function loadHome() {
  if (!summaryData) {
    try { const r=await fetch('/api/summary'); summaryData=await r.json(); } catch(e) { document.getElementById('home-stats').innerHTML='<div class="empty">Could not load stats.</div>'; return; }
  }
  const d = summaryData;
  document.getElementById('home-stats').innerHTML = [
    {v:d.discovered,l:'Discovered',cls:'dim'},
    {v:d.eligible,l:'Eligible',cls:'lav'},
    {v:d.prioritized,l:'Prioritized',cls:'pink'},
    {v:d.attacks_ready,l:'Attacks Ready',cls:'green'},
    {v:d.p1,l:'P1 — DEEP',cls:'pink'},
    {v:d.p2,l:'P2 — STANDARD',cls:'lav'},
    {v:d.contacts_found,l:'Contacts Found',cls:'green'},
    {v:d.new_today,l:'New Today',cls:'dim'},
  ].map(c=>`<div class="stat-card ${c.cls}"><div class="stat-val mono">${c.v??'–'}</div><div class="stat-label">${c.l}</div></div>`).join('');
  const lr = d.last_run ? ' · Last updated ' + fmtDate(d.last_run) : '';
  document.getElementById('home-today-text').textContent = `${d.new_today||0} new roles found today${lr}`;
}

// ---- AGENT ----
async function loadAgent() {
  if (!oppsData) {
    try { const r=await fetch('/api/opportunities'); oppsData=await r.json(); } catch(e) { document.getElementById('agent-list').innerHTML='<div class="empty">Could not load.</div>'; return; }
  }
  renderAgentList();
  if (selectedJobId) {
    const j = oppsData.find(j=>j.job_id===selectedJobId);
    if (j) renderJobDetail(j);
  }
}

function renderAgentList() {
  if (!oppsData) return;
  const filtered = agentFilter==='ALL' ? oppsData : oppsData.filter(j=>j.attack_priority===agentFilter);
  if (!filtered.length) {
    document.getElementById('agent-list').innerHTML='<div class="empty">No opportunities match this filter.</div>';
    return;
  }
  document.getElementById('agent-list').innerHTML = filtered.map(j => {
    const p = (j.attack_priority||'').toLowerCase();
    const fit = j.overall_fit != null ? `<span class="fit-num">${j.overall_fit}/10</span>` : '';
    return `<div class="opp-card${j.job_id===selectedJobId?' selected':''}" onclick="selectJob('${esc(j.job_id)}')">
      <div class="opp-title">${esc(j.title)}</div>
      <div class="opp-company">${esc(j.company)}</div>
      <div class="opp-meta">
        <span class="p-chip ${p}">${esc(j.attack_priority)}</span>
        ${j.attack_intensity ? `<span style="font-size:10px;color:var(--text-dim);">${esc(j.attack_intensity)}</span>` : ''}
        ${fit}
      </div>
    </div>`;
  }).join('');
}

function selectJob(id) {
  selectedJobId = id;
  const j = oppsData && oppsData.find(j=>j.job_id===id);
  if (!j) return;
  renderAgentList();
  renderJobDetail(j);
}

function renderJobDetail(j) {
  const p = (j.attack_priority||'').toLowerCase();
  let execHtml = '';
  let execPkt = null;
  if (j.execution_packet) { try { execPkt = JSON.parse(j.execution_packet); } catch(e){} }

  let seqHtml = '';
  if (j.attack_action_sequence) {
    let seq = j.attack_action_sequence;
    if (typeof seq === 'string') { try { seq = JSON.parse(seq); } catch(e){ seq=[]; } }
    if (Array.isArray(seq) && seq.length) {
      seqHtml = seq.map((s,i) =>
        `${i>0?'<span class="seq-arrow">→</span>':''}
        <span class="seq-step"><span class="seq-num">${i+1}</span>${esc(s)}</span>`
      ).join('');
    }
  }

  let fitHtml = '';
  const dims = [
    {key:'role_fit',label:'Role Fit'},{key:'experience_fit',label:'Exp Fit'},
    {key:'skill_fit',label:'Skill Fit'},{key:'portfolio_fit',label:'Portfolio'},
    {key:'domain_fit',label:'Domain Fit'},
  ];
  const hasFit = dims.some(d=>j[d.key]!=null);
  if (hasFit) {
    fitHtml = `<div class="detail-section">
      <div class="detail-section-title">Fit Assessment</div>
      ${dims.map(d=>j[d.key]!=null?`<div class="fit-row">
        <span class="fit-label">${d.label}</span>
        <div class="fit-track"><div class="fit-fill" style="width:${(j[d.key]/10)*100}%"></div></div>
        <span class="fit-score">${j[d.key]}</span>
      </div>`:'').join('')}
    </div>`;
  }

  if (execPkt && execPkt.steps && execPkt.steps.length) {
    execHtml = `<div class="detail-section">
      <div class="detail-section-title">Execution Plan${seqHtml?'<span style="float:right;display:flex;gap:4px;align-items:center;">'+seqHtml+'</span>':''}</div>
      ${execPkt.steps.map(s=>renderExecStep(j.job_id,s,j)).join('')}
      <div style="margin-top:10px;display:flex;gap:8px;">
        <button class="btn-sm primary" style="flex:1;" onclick="markSentFromAgent('${esc(j.job_id)}', this)">Mark as Sent</button>
      </div>
    </div>`;
  } else {
    execHtml = `<div class="detail-section">
      <div class="detail-section-title">Execution Plan</div>
      <div class="empty" style="padding:16px 0;">No execution packet built yet — run the pipeline to generate it.</div>
    </div>`;
  }

  document.getElementById('agent-detail').innerHTML = `
    <div class="detail-priority"><span class="p-chip ${p}">${esc(j.attack_priority)}</span>${j.attack_intensity?` <span style="font-size:11px;color:var(--text-dim);margin-left:6px;">${esc(j.attack_intensity)}</span>`:''}</div>
    <div class="detail-title">${esc(j.title)}</div>
    <div class="detail-company">${esc(j.company)}${j.location?' · '+esc(j.location):''}</div>
    <div style="margin-bottom:16px;">
      ${j.url?`<a href="${esc(j.url)}" target="_blank" class="btn-sm">View Posting ↗</a> `:''}
      ${j.company_linkedin?`<a href="${esc(j.company_linkedin)}" target="_blank" class="btn-sm">LinkedIn ↗</a>`:''}
      ${j.hm_email?`<a href="mailto:${esc(j.hm_email)}" class="btn-sm">${esc(j.hm_email)}</a>`:''}
    </div>
    ${j.attack_reason||j.eligibility_reason?`<div class="detail-section">
      <div class="detail-section-title">Why This Job</div>
      <div class="why-text">${esc(j.attack_reason||j.eligibility_reason)}</div>
    </div>`:''}
    ${fitHtml}
    ${execHtml}
  `;
}

// ---- Agent filter pills ----
document.querySelectorAll('.p-pill').forEach(btn => {
  btn.addEventListener('click', () => {
    agentFilter = btn.dataset.p;
    document.querySelectorAll('.p-pill').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    selectedJobId = null;
    renderAgentList();
    document.getElementById('agent-detail').innerHTML='<div class="detail-empty">Select a job to see the full attack plan.</div>';
  });
});

async function markSentFromAgent(jobId, btn) {
  const pk = kpiPasskey || getSavedPk();
  if (!pk) {
    const entered = prompt('Enter passkey to log this event:');
    if (!entered) return;
    kpiPasskey = entered;
    savePk(entered);
  }
  btn.disabled = true;
  btn.textContent = 'Logging…';
  try {
    const res = await fetch('/api/pipeline/log', {
      method:'POST',
      headers:{'Authorization':'Bearer '+(kpiPasskey||getSavedPk()),'Content-Type':'application/json'},
      body: JSON.stringify({job_id:jobId,event_type:'email_sent',action:'set',noted_at:new Date().toISOString()}),
    });
    if (res.status===401) { btn.textContent='Wrong passkey'; btn.disabled=false; kpiPasskey=''; return; }
    if (res.ok) { btn.textContent='Logged!'; btn.style.color='var(--green)'; }
    else { const e=await res.json().catch(()=>({})); btn.textContent='Error: '+(e.error||res.status); }
  } catch(e) { btn.textContent='Network error'; }
  setTimeout(()=>{btn.disabled=false;btn.textContent='Mark as Sent';btn.style.color='';},2500);
}

// ---- EXECUTION STEP RENDERER (shared) ----
function renderExecStep(jobId, step, job) {
  const num = step.step||1, type = step.type||'none', label = step.label||type;
  const sid = `${jobId}-${num}`;
  let body = '';
  if (type==='linkedin') {
    const pl=[step.person_name,step.person_role].filter(Boolean).join(' · ');
    const draft=step.draft||(job&&job.linkedin_draft)||'';
    body=(pl?`<div class="exec-person">${esc(pl)}</div>`:'')
      +(step.linkedin_url?`<a href="${esc(step.linkedin_url)}" target="_blank" class="btn-sm" style="margin-bottom:6px;display:inline-flex;" onclick="event.stopPropagation()">Open LinkedIn ↗</a><br>`:'')
      +(draft?`<pre id="xd-${sid}" style="display:none">${esc(draft)}</pre><div class="draft-box">${esc(draft)}</div><div class="exec-btns"><button class="btn-sm primary" onclick="event.stopPropagation();copyText(document.getElementById('xd-${sid}').textContent,this,'Copy DM')">Copy DM</button></div>`:'');
  } else if (type==='email'||type==='email_unattributed') {
    const email=step.recipient_email||'', subj=step.subject||'', draft=step.draft||'';
    const note=step.note||'';
    body=(email?`<div class="exec-email">${esc(email)}</div>`:'')
      +(subj?`<div class="exec-subject">Subject: ${esc(subj)}</div>`:'')
      +(draft?`<pre id="xd-${sid}" style="display:none">${esc(draft)}</pre>`
            +`<pre id="xs-${sid}" style="display:none">${esc(subj)}</pre>`
            +`<pre id="xe-${sid}" style="display:none">${esc(email)}</pre>`
            +`<div class="draft-box">${esc(draft)}</div>`
            +`<div class="exec-btns">`
            +(subj?`<button class="btn-sm" onclick="event.stopPropagation();copyText(document.getElementById('xs-${sid}').textContent,this,'Copy Subj')">Copy Subject</button>`:'')
            +(email?`<button class="btn-sm" onclick="event.stopPropagation();copyText(document.getElementById('xe-${sid}').textContent,this,'Copy Email')">Copy Email</button>`:'')
            +`<button class="btn-sm primary" onclick="event.stopPropagation();copyText(document.getElementById('xd-${sid}').textContent,this,'Copy Body')">Copy Body</button></div>`
          :(note?`<div style="font-size:11.5px;color:var(--text-muted);">${esc(note)}</div>`:''));
  } else if (type==='apply') {
    body=step.url?`<a href="${esc(step.url)}" target="_blank" class="btn-sm primary" onclick="event.stopPropagation()">Open Application ↗</a>`
                :`<div style="font-size:12px;color:var(--text-muted);">No application link available.</div>`;
  } else {
    body=`<div style="font-size:12px;color:var(--text-muted);">${esc(label)}</div>`;
  }
  return `<div class="exec-step"><div class="exec-step-hdr"><span class="exec-step-num">${num}</span><span class="exec-step-lbl">${esc(label)}</span></div><div>${body}</div></div>`;
}

// ---- ACTIONS ----
async function loadActions() {
  const wrap = document.getElementById('actions-container');
  if (!oppsData) {
    wrap.innerHTML='<div class="loading">Loading…</div>';
    try { const r=await fetch('/api/opportunities'); oppsData=await r.json(); } catch(e){ wrap.innerHTML='<div class="empty">Could not load.</div>'; return; }
  }
  // Try to load pipeline state if passkey available
  const pk = kpiPasskey || getSavedPk();
  let pipeState = null;
  if (pk && !kpiData) {
    try {
      const r=await fetch('/api/pipeline/state',{headers:{'Authorization':'Bearer '+pk}});
      if (r.ok) { kpiData=await r.json(); kpiPasskey=pk; }
    } catch(e){}
  }
  if (kpiData) pipeState = kpiData;
  renderActions(pipeState);
}

function renderActions(pipeState) {
  const wrap = document.getElementById('actions-container');
  const opps = oppsData || [];

  // Build pipeline state lookup
  const stateMap = {};
  const followupDue = [];
  const inprogress = [];
  const responses = [];
  if (pipeState) {
    for (const j of pipeState.jobs) {
      stateMap[j.job_id] = j;
      if (j.followup_due) followupDue.push(j);
      else if (['RESPONDED','CONVERSATION','INTERVIEW','OFFER'].includes(j.state)) responses.push(j);
      else if (['WAITING','FOLLOW-UP DUE','NO RESPONSE'].includes(j.state)) inprogress.push(j);
    }
  }

  // NEW ACTIONS: opps with execution packet not yet in pipeline
  const newActions = opps.filter(j => {
    if (!j.execution_packet) return false;
    if (!pipeState) return true;
    const s = stateMap[j.job_id];
    return !s || s.state === 'NOT STARTED';
  });

  let html = '';

  if (!pipeState) {
    html += `<div style="background:var(--lavender-dark);border:1px solid var(--lavender-border);border-radius:10px;padding:14px;margin-bottom:24px;font-size:13px;color:var(--lavender);">
      Unlock KPIs to see your pipeline groups (responses, follow-ups, in progress). New actions are shown below.
    </div>`;
  }

  if (responses.length) {
    html += `<div class="group-block"><div class="group-title response">Responses <span class="group-count response">${responses.length}</span></div>
      ${responses.map(j=>actionCardHtml(j,null,'response')).join('')}</div>`;
  }
  if (followupDue.length) {
    html += `<div class="group-block"><div class="group-title followup">Follow-ups Due <span class="group-count followup">${followupDue.length}</span></div>
      ${followupDue.map(j=>actionCardHtml(j,null,'followup')).join('')}</div>`;
  }
  if (inprogress.length) {
    html += `<div class="group-block"><div class="group-title inprogress">Attacks in Progress <span class="group-count inprogress">${inprogress.length}</span></div>
      ${inprogress.map(j=>actionCardHtml(j,null,'inprogress')).join('')}</div>`;
  }
  if (newActions.length) {
    html += `<div class="group-block"><div class="group-title newactions">New Actions <span class="group-count newactions">${newActions.length}</span></div>
      ${newActions.map(j=>actionCardHtml(null,j,'newactions')).join('')}</div>`;
  }
  if (!html) html = '<div class="empty">No actions to show. Run the pipeline to generate execution packets.</div>';

  wrap.innerHTML = html;
  wrap.querySelectorAll('.action-card-hdr').forEach(hdr => {
    hdr.addEventListener('click', ()=>hdr.closest('.action-card').classList.toggle('open'));
  });
}

function actionCardHtml(pipeJob, oppJob, cls) {
  const j = oppJob || (oppsData && oppsData.find(o=>o.job_id===(pipeJob&&pipeJob.job_id))) || pipeJob;
  if (!j) return '';
  const title = j.title||pipeJob&&pipeJob.title||'';
  const company = j.company||pipeJob&&pipeJob.company||'';
  const jid = j.job_id;
  let bodyHtml = '';
  let execPkt = null;
  if (j.execution_packet) { try{execPkt=JSON.parse(j.execution_packet);}catch(e){} }
  if (execPkt && execPkt.steps) {
    bodyHtml = execPkt.steps.map(s=>renderExecStep(jid,s,j)).join('');
    bodyHtml += `<div style="display:flex;gap:8px;margin-top:10px;">
      <button class="btn-sm primary" style="flex:1;" onclick="logActionEvent('${esc(jid)}','email_sent',this)">Mark Email Sent</button>
      <button class="btn-sm" style="flex:1;" onclick="logActionEvent('${esc(jid)}','linkedin_sent',this)">Mark LinkedIn Sent</button>
    </div>`;
  } else if (pipeJob) {
    const stateClass = 's-'+((pipeJob.state||'NOT STARTED').replace(/\s/g,'\\ '));
    bodyHtml = `<span class="state-badge ${stateClass}">${esc(pipeJob.state)}</span>`;
  } else {
    bodyHtml = '<div class="empty" style="padding:10px 0;">No execution packet — run pipeline.</div>';
  }
  return `<div class="action-card" id="ac-${jid}">
    <div class="action-card-hdr">
      <div class="action-title-wrap">
        <div class="action-title">${esc(title)}</div>
        <div class="action-company">${esc(company)}</div>
      </div>
      ${j.attack_priority?`<span class="p-chip ${(j.attack_priority||'').toLowerCase()}">${esc(j.attack_priority)}</span>`:''}
      <span class="action-toggle">▼</span>
    </div>
    <div class="action-card-body">${bodyHtml}</div>
  </div>`;
}

async function logActionEvent(jobId, eventType, btn) {
  let pk = kpiPasskey || getSavedPk();
  if (!pk) {
    pk = prompt('Enter passkey to log this event:');
    if (!pk) return;
    kpiPasskey = pk; savePk(pk);
  }
  btn.disabled = true; btn.textContent = 'Logging…';
  try {
    const res = await fetch('/api/pipeline/log', {
      method:'POST',
      headers:{'Authorization':'Bearer '+pk,'Content-Type':'application/json'},
      body: JSON.stringify({job_id:jobId,event_type:eventType,action:'set',noted_at:new Date().toISOString()}),
    });
    if (res.status===401){btn.textContent='Wrong passkey';kpiPasskey='';btn.disabled=false;return;}
    if (res.ok){btn.textContent='Logged!';btn.style.color='var(--green)';kpiData=null;}
    else{const e=await res.json().catch(()=>({}));btn.textContent='Error: '+(e.error||res.status);}
  } catch(e){btn.textContent='Network error';}
  setTimeout(()=>{btn.disabled=false;btn.textContent=eventType==='email_sent'?'Mark Email Sent':'Mark LinkedIn Sent';btn.style.color='';},2500);
}

// ---- PIPELINE ----
async function loadPipeline() {
  if (!summaryData) {
    try{const r=await fetch('/api/summary');summaryData=await r.json();}catch(e){document.getElementById('pipeline-funnel').innerHTML='<div class="empty">Could not load.</div>';return;}
  }
  const d = summaryData;
  const stages = [
    {v:d.discovered,l:'Discovered',r:''},
    {v:d.eligible,l:'Eligible',r:d.discovered?Math.round(d.eligible/d.discovered*100)+'%':'—'},
    {v:d.prioritized,l:'Prioritized',r:d.eligible?Math.round(d.prioritized/d.eligible*100)+'%':'—'},
    {v:d.attacks_ready,l:'Attacks Ready',r:d.prioritized?Math.round(d.attacks_ready/d.prioritized*100)+'%':'—'},
  ];
  document.getElementById('pipeline-funnel').innerHTML = stages.map((s,i) =>
    `${i>0?'<div class="funnel-arrow">→</div>':''}
    <div class="funnel-card">
      <div class="funnel-val mono">${s.v??'–'}</div>
      <div class="funnel-label">${s.l}</div>
      ${s.r?`<div class="funnel-rate">${s.r} of previous</div>`:''}
    </div>`
  ).join('');

  const srcs = d.sources||{};
  const srcLabels = {hackernews:'Hacker News',cutshort:'Cutshort',iimjobs:'iimjobs',google_jobs:'Google Jobs',internshala:'Internshala',jsearch:'JSearch',yc:'YC Jobs',careers:'Direct Careers'};
  const srcEntries = Object.entries(srcs).sort((a,b)=>b[1]-a[1]);
  document.getElementById('pipeline-sources').innerHTML = srcEntries.length
    ? srcEntries.map(([src,cnt])=>`<tr><td class="name">${esc(srcLabels[src]||src)}</td><td class="num">${cnt}</td></tr>`).join('')
    : '<tr><td colspan="2" class="empty" style="text-align:center;">No eligible jobs yet.</td></tr>';
}

// ---- KPI ----
function initKpi() {
  const pk = kpiPasskey || getSavedPk();
  if (pk) { kpiPasskey=pk; loadKpiData(); }
  else {
    document.getElementById('kpi-gate-wrap').style.display='flex';
    document.getElementById('kpi-content').style.display='none';
  }
}

async function kpiUnlock() {
  const val = document.getElementById('kpi-pk').value.trim();
  if (!val) {document.getElementById('kpi-err').textContent='Enter a passkey.';return;}
  kpiPasskey = val;
  await loadKpiData(true);
}

async function loadKpiData(fromUnlock=false) {
  try {
    const res = await fetch('/api/pipeline/state',{headers:{'Authorization':'Bearer '+kpiPasskey}});
    if (res.status===401) {
      if (fromUnlock) document.getElementById('kpi-err').textContent='Wrong passkey.';
      kpiPasskey='';
      return;
    }
    kpiData = await res.json();
    savePk(kpiPasskey);
    document.getElementById('kpi-gate-wrap').style.display='none';
    document.getElementById('kpi-content').style.display='block';
    renderKpiMetrics(kpiData.metrics);
    renderKpiConv(kpiData.metrics);
    renderKpiAtt(kpiData.jobs);
    renderKpiList();
  } catch(e) { if (fromUnlock) document.getElementById('kpi-err').textContent='Connection failed.'; }
}

function kpiLock() {
  kpiPasskey=''; kpiData=null;
  try{sessionStorage.removeItem('rr_pk');}catch(e){}
  document.getElementById('kpi-gate-wrap').style.display='flex';
  document.getElementById('kpi-content').style.display='none';
  document.getElementById('kpi-pk').value='';
}

function renderKpiMetrics(m) {
  const cards=[
    {v:m.applications_sent,l:'Applications',cls:'lav'},{v:m.linkedin_sent,l:'LinkedIn Sent',cls:'lav'},
    {v:m.emails_sent,l:'Emails Sent',cls:'pink'},{v:m.followups_sent,l:'Follow-ups',cls:'pink'},
    {v:m.responses,l:'Responses',cls:'green'},{v:m.conversations,l:'Conversations',cls:'green'},
    {v:m.interviews,l:'Interviews',cls:'yellow'},{v:m.offers,l:'Offers',cls:'yellow'},
  ];
  document.getElementById('kpi-metrics').innerHTML=cards.map(c=>`<div class="metric-card ${c.cls}"><div class="metric-val">${c.v}</div><div class="metric-lbl">${c.l}</div></div>`).join('');
}

function renderKpiConv(m) {
  const rows=[
    {r:m.application_to_response_rate,l:'Application → Response'},
    {r:m.response_to_conversation_rate,l:'Response → Conversation'},
    {r:m.conversation_to_interview_rate,l:'Conversation → Interview'},
    {r:m.interview_to_offer_rate,l:'Interview → Offer'},
  ];
  document.getElementById('kpi-conv').innerHTML=rows.map(r=>
    `<div class="conv-card">${r.r!==null&&r.r!==undefined?`<div class="conv-rate">${r.r}%</div>`:`<div class="conv-null">&mdash;</div>`}<div class="conv-lbl">${r.l}</div></div>`
  ).join('');
}

function renderKpiAtt(jobs) {
  const due=jobs.filter(j=>j.followup_due);
  if (!due.length){document.getElementById('kpi-att').innerHTML='';return;}
  document.getElementById('kpi-att').innerHTML=`<div class="att-block"><div class="att-title">Follow-up Due (${due.length})</div>
    ${due.map(j=>`<div class="att-row"><span class="att-company">${esc(j.company)}</span><span class="att-title-text">${esc(j.title)}</span>
      <button class="event-btn due" onclick="kpiLogEvent('${j.job_id}','followup_sent',this)">Mark Follow-up Sent</button>
    </div>`).join('')}
  </div>`;
}

const EV_ORDER=['applied','linkedin_sent','email_sent','followup_sent','response','conversation','interview','rejected','offer'];
const EV_LABELS={applied:'Applied',linkedin_sent:'LinkedIn Sent',email_sent:'Email Sent',followup_sent:'Follow-up Sent',response:'Response',conversation:'Conversation',interview:'Interview',rejected:'Rejected',offer:'Offer'};
const EV_NEG=new Set(['rejected']);
const EV_GOLD=new Set(['offer','interview']);
const STATE_PRIO={'OFFER':1,'INTERVIEW':2,'CONVERSATION':3,'RESPONDED':4,'FOLLOW-UP DUE':5,'WAITING':6,'APPLICATION SENT':7,'REJECTED':8,'NO RESPONSE':9,'NOT STARTED':10};

function renderKpiList() {
  if (!kpiData) return;
  const q=(document.getElementById('kpi-search').value||'').toLowerCase();
  const jobs=[...kpiData.jobs].filter(j=>!q||j.title.toLowerCase().includes(q)||j.company.toLowerCase().includes(q))
    .sort((a,b)=>{const pa=STATE_PRIO[a.state]||99,pb=STATE_PRIO[b.state]||99;if(pa!==pb)return pa-pb;const ap={P1:1,P2:2,P3:3}[a.attack_priority]||9,bp={P1:1,P2:2,P3:3}[b.attack_priority]||9;return ap-bp;});
  if (!jobs.length){document.getElementById('kpi-list').innerHTML='<div class="empty">No jobs match.</div>';return;}
  document.getElementById('kpi-list').innerHTML=jobs.map(j=>{
    const sc='s-'+j.state;
    const btns=EV_ORDER.map(et=>{
      const active=j[et],neg=EV_NEG.has(et)&&active?' neg':'',gold=EV_GOLD.has(et)&&active?' gold':'',due=et==='followup_sent'&&j.followup_due&&!active?' due':'';
      const d=active&&j.events_at&&j.events_at[et]?fmtDate(j.events_at[et]):'';
      return `<button class="event-btn${active?' active'+neg+gold:due}" onclick="kpiLogEvent('${j.job_id}','${et}',this)" title="${active?'Logged '+d+' — click to remove':'Mark done'}">${EV_LABELS[et]}${active&&d?' · '+d:''}</button>`;
    }).join('');
    return `<div class="pipe-job" id="pj-${j.job_id}">
      <div class="pipe-job-top">
        <div style="flex:1;min-width:200px;"><div class="pipe-job-title">${esc(j.title)}</div><div class="pipe-job-co">${esc(j.company)}${j.location?' · '+esc(j.location):''}</div></div>
        <span class="state-badge ${sc}">${esc(j.state)}</span>
      </div>
      <div class="event-btns">${btns}</div>
    </div>`;
  }).join('');
}

async function kpiLogEvent(jobId, eventType, btn) {
  if (!kpiData) return;
  const job = kpiData.jobs.find(j=>j.job_id===String(jobId));
  if (!job) return;
  const isActive = job[eventType];
  const action = isActive?'unset':'set';
  if (isActive && !confirm(`Remove "${EV_LABELS[eventType]}" from this job?`)) return;
  btn.disabled=true;
  try {
    const res=await fetch('/api/pipeline/log',{
      method:'POST',
      headers:{'Authorization':'Bearer '+kpiPasskey,'Content-Type':'application/json'},
      body:JSON.stringify({job_id:jobId,event_type:eventType,action,noted_at:new Date().toISOString()}),
    });
    if (res.status===401){kpiLock();return;}
    if (!res.ok){const e=await res.json().catch(()=>({}));alert('Error: '+(e.error||res.status));return;}
    await loadKpiData();
  } catch(e){alert('Network error.');}
  finally{btn.disabled=false;}
}

// ---- Boot ----
loadHome();
</script>
</body>
</html>

"""


if __name__ == "__main__":
    database.init_db()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
