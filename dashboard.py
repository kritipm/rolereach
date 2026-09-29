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
            " WHERE (eligibility_status = 'ELIGIBLE' AND attack_priority IS NOT NULL)"
            "    OR (eligibility_status = 'REVIEW')"
            " ORDER BY"
            " CASE eligibility_status WHEN 'ELIGIBLE' THEN 0 ELSE 1 END,"
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
  --review: #DDB0FF; --review-dark: #140C2C; --review-border: #201848;
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

/* state badge variants for clean class names */
.s-offer{background:#1A1400;border:1px solid #504000;color:#F0C040;}
.s-interview{background:var(--p1-dark);border:1px solid var(--p1-border);color:var(--p1);box-shadow:0 0 8px var(--p1-glow);}
.s-conversation,.s-responded{background:var(--green-dark);border:1px solid var(--green-border);color:var(--green);}
.s-rejected{background:var(--red-dark);border:1px solid var(--red-border);color:var(--red);}
.s-followup-due{background:var(--red-dark);border:1px solid var(--red-border);color:var(--red);animation:pulseRed 2s infinite;}
.s-no-response{background:rgba(60,20,30,0.5);border:1px solid var(--red-border);color:#A04060;}
.s-waiting{background:var(--yellow-dark);border:1px solid var(--yellow-border);color:var(--yellow);}
.s-app-sent{background:var(--lavender-dark);border:1px solid var(--lavender-border);color:var(--lavender);}
.s-not-started{background:transparent;border:1px solid var(--border);color:var(--text-dim);}

/* ---- AGENT extras ---- */
.p-pill.review.active,.p-pill.review:hover{background:var(--review-dark);border-color:var(--review-border);color:var(--review);}
.opp-freshness{font-size:11px;color:var(--text-dim);}
.opp-why{font-size:11.5px;color:var(--text-muted);line-height:1.4;margin-top:5px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
.opp-seq{display:flex;gap:4px;align-items:center;margin-top:4px;flex-wrap:wrap;}
.seq-tag{font-size:10px;font-weight:700;padding:2px 6px;border-radius:4px;background:rgba(200,48,240,0.08);border:1px solid var(--p1-border);color:var(--p1);}
.seq-arr{font-size:10px;color:var(--text-dim);}
.review-chip-card{display:inline-flex;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:800;background:var(--review-dark);border:1px solid var(--review-border);color:var(--review);}
.access-badge{display:inline-flex;padding:2px 7px;border-radius:999px;font-size:10px;font-weight:800;border:1px solid;}
.access-HIGH{background:var(--p1-dark);border-color:var(--p1-border);color:var(--p1);}
.access-MEDIUM{background:var(--p2-dark);border-color:var(--p2-border);color:var(--p2);}
.access-LOW{background:var(--p3-dark);border-color:var(--p3-border);color:var(--text-muted);}
.access-NONE{background:transparent;border-color:var(--border);color:var(--text-dim);}
/* detail sections */
.detail-meta-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:14px;font-size:12px;color:var(--text-muted);}
.detail-meta-dot{color:var(--text-dim);}
.contact-block{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px 14px;margin-bottom:8px;}
.contact-name{font-size:13px;font-weight:700;color:var(--text-primary);}
.contact-role{font-size:12px;color:var(--text-muted);margin-bottom:4px;}
.contact-email{font-size:12px;color:var(--text-muted);font-family:"SF Mono",Consolas,monospace;}
.detail-collapse-hdr{display:flex;align-items:center;justify-content:space-between;cursor:pointer;padding:4px 0;}
.detail-collapse-hdr:hover{color:var(--lavender);}
.detail-collapse-icon{font-size:11px;color:var(--text-dim);transition:transform 0.15s;}
.detail-collapse-body{display:none;margin-top:10px;font-size:12px;color:var(--text-secondary);line-height:1.65;white-space:pre-wrap;}
.detail-collapse-body.open{display:block;}
/* review detail */
.review-banner{background:var(--review-dark);border:1px solid var(--review-border);border-radius:10px;padding:12px 14px;margin-bottom:14px;}
.review-banner-title{font-size:11px;font-weight:800;color:var(--review);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;}
.review-banner-text{font-size:13px;color:var(--text-secondary);line-height:1.5;}
/* ---- ACTIONS extras ---- */
.group-title.completed{color:var(--text-dim);}
.group-count.completed{background:rgba(80,80,96,0.2);border:1px solid var(--border);color:var(--text-dim);}
.catchup{text-align:center;padding:48px 20px;color:var(--text-dim);font-size:14px;}
.catchup-icon{font-size:28px;margin-bottom:10px;}
/* ---- PIPELINE extras ---- */
.funnel-full{margin-bottom:24px;}
.funnel-row{display:flex;align-items:stretch;gap:0;overflow-x:auto;padding-bottom:8px;margin-bottom:8px;}
.funnel-stage{flex:1;min-width:90px;background:var(--card);border:1px solid var(--border);border-right:none;padding:14px 12px;cursor:pointer;transition:background 0.15s,border-color 0.15s;position:relative;}
.funnel-stage:first-child{border-radius:10px 0 0 10px;}
.funnel-stage:last-child{border-right:1px solid var(--border);border-radius:0 10px 10px 0;}
.funnel-stage:hover{background:var(--card-hover);}
.funnel-stage.active{background:var(--p1-dark);border-color:var(--p1-border);}
.funnel-stage.active .funnel-sval{color:var(--p1);}
.funnel-sval{font-size:22px;font-weight:900;line-height:1;color:var(--lavender);}
.funnel-slbl{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:0.4px;color:var(--text-muted);margin-top:3px;}
.funnel-srate{font-size:10px;color:var(--text-dim);margin-top:2px;}
.funnel-branches{display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap;}
.branch-card{flex:1;min-width:120px;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 14px;cursor:pointer;transition:background 0.15s;}
.branch-card.review:hover,.branch-card.review.active{background:var(--review-dark);border-color:var(--review-border);}
.branch-card.rejected:hover,.branch-card.rejected.active{background:var(--red-dark);border-color:var(--red-border);}
.branch-card.review .branch-val,.branch-card.review.active .branch-val{color:var(--review);}
.branch-card.rejected .branch-val,.branch-card.rejected.active .branch-val{color:var(--red);}
.branch-val{font-size:22px;font-weight:900;line-height:1;color:var(--text-muted);}
.branch-lbl{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:0.4px;color:var(--text-muted);margin-top:3px;}
.stage-jobs-panel{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:20px;}
.stage-jobs-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;}
.stage-jobs-title{font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted);}
.stage-job-row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);}
.stage-job-row:last-child{border-bottom:none;}
.stage-job-title{font-size:13px;font-weight:700;flex:1;min-width:0;}
.stage-job-co{font-size:11px;color:var(--text-muted);}
.stage-job-chip{flex-shrink:0;}
/* ---- KPI extras ---- */
.attack-exec-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:24px;}
.attack-exec-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px;}
.attack-exec-val{font-size:26px;font-weight:900;line-height:1;}
.attack-exec-lbl{font-size:11px;color:var(--text-muted);font-weight:700;text-transform:uppercase;letter-spacing:0.5px;margin-top:4px;}
.attack-exec-card.pink .attack-exec-val{color:var(--pink);}
.attack-exec-card.lav .attack-exec-val{color:var(--lavender);}
.attack-exec-card.green .attack-exec-val{color:var(--green);}
.attack-exec-card.dim .attack-exec-val{color:var(--text-muted);}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;}}
.tab-footer-note{position:fixed;bottom:0;left:0;right:0;padding:6px 16px;background:var(--bg);border-top:1px solid var(--border);font-size:11px;color:var(--text-dim);text-align:center;z-index:50;pointer-events:none;}
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
    <button class="p-pill review" data-p="REVIEW">REVIEW</button>
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
      <div class="kpi-section-title">Attack Execution</div>
      <div id="kpi-attack-exec" class="attack-exec-grid"></div>
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
let pipeStageOpen = null;

// ---- Utilities ----
function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function fmtDate(iso) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleDateString('en-IN',{day:'numeric',month:'short'}); } catch(e){return '';}
}
function freshness(iso) {
  if (!iso) return '';
  try {
    const days = Math.floor((Date.now() - new Date(iso)) / 86400000);
    if (days <= 0) return 'today';
    if (days === 1) return '1 day ago';
    if (days < 7) return days + ' days ago';
    if (days < 14) return '1 week ago';
    if (days < 30) return Math.floor(days/7) + ' weeks ago';
    return Math.floor(days/30) + 'mo ago';
  } catch(e) { return ''; }
}
function seqPreview(seq) {
  if (!seq) return '';
  let arr = seq;
  if (typeof arr === 'string') { try { arr = JSON.parse(arr); } catch(e) { return ''; } }
  if (!Array.isArray(arr) || !arr.length) return '';
  return arr;
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
  let filtered;
  if (agentFilter === 'ALL') {
    filtered = oppsData.filter(j => j.eligibility_status === 'ELIGIBLE');
  } else if (agentFilter === 'REVIEW') {
    filtered = oppsData.filter(j => j.eligibility_status === 'REVIEW');
  } else {
    filtered = oppsData.filter(j => j.attack_priority === agentFilter);
  }
  if (!filtered.length) {
    document.getElementById('agent-list').innerHTML='<div class="empty">No opportunities match this filter.</div>';
    return;
  }
  document.getElementById('agent-list').innerHTML = filtered.map(j => {
    const isReview = j.eligibility_status === 'REVIEW';
    const p = (j.attack_priority||'').toLowerCase();
    const fit = j.overall_fit != null ? j.overall_fit : null;
    const fr = freshness(j.posted_at);
    const loc = j.location && j.location !== 'Location not specified' ? j.location : '';
    const why = j.attack_reason || j.eligibility_reason || '';
    const seq = seqPreview(j.attack_action_sequence);
    const access = j.attack_access_level || '';

    if (isReview) {
      return `<div class="opp-card${j.job_id===selectedJobId?' selected':''}" onclick="selectJob('${esc(j.job_id)}')">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">
          <span class="review-chip-card">REVIEW</span>
        </div>
        <div class="opp-title">${esc(j.title)}</div>
        <div class="opp-company">${esc(j.company)}</div>
        <div class="opp-meta">
          ${loc ? `<span class="opp-freshness">${esc(loc)}</span>` : ''}
          ${loc && fr ? `<span class="opp-freshness">·</span>` : ''}
          ${fr ? `<span class="opp-freshness">${esc(fr)}</span>` : ''}
        </div>
        ${why ? `<div class="opp-why">${esc(why)}</div>` : ''}
      </div>`;
    }

    return `<div class="opp-card${j.job_id===selectedJobId?' selected':''}" onclick="selectJob('${esc(j.job_id)}')">
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">
        ${j.attack_priority ? `<span class="p-chip ${p}">${esc(j.attack_priority)}</span>` : ''}
        ${j.attack_intensity ? `<span style="font-size:10px;color:var(--text-dim);">${esc(j.attack_intensity)}</span>` : ''}
      </div>
      <div class="opp-title">${esc(j.title)}</div>
      <div class="opp-company">${esc(j.company)}</div>
      <div class="opp-meta">
        ${loc ? `<span class="opp-freshness">${esc(loc)}</span>` : ''}
        ${loc && fr ? `<span class="opp-freshness">·</span>` : ''}
        ${fr ? `<span class="opp-freshness">${esc(fr)}</span>` : ''}
        ${fit != null ? `<span class="fit-num" style="margin-left:4px;">Fit ${fit}/10</span>` : ''}
        ${access ? `<span class="access-badge access-${esc(access)}" style="margin-left:2px;">${esc(access)}</span>` : ''}
      </div>
      ${why ? `<div class="opp-why">${esc(why)}</div>` : ''}
      ${Array.isArray(seq) && seq.length ? `<div class="opp-seq">${seq.map((s,i)=>(i>0?'<span class="seq-arr">→</span>':'')+'<span class="seq-tag">'+esc(s)+'</span>').join('')}</div>` : ''}
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
  const isReview = j.eligibility_status === 'REVIEW';
  const p = (j.attack_priority||'').toLowerCase();
  const fr = freshness(j.posted_at);
  const loc = j.location && j.location !== 'Location not specified' ? j.location : '';

  // REVIEW job: simplified detail
  if (isReview) {
    const jdText = j.description || j.text || '';
    document.getElementById('agent-detail').innerHTML = `
      <div style="margin-bottom:14px;"><span class="review-chip-card">REVIEW</span></div>
      <div class="detail-title">${esc(j.title)}</div>
      <div class="detail-company">${esc(j.company)}</div>
      <div class="detail-meta-row">
        ${loc ? `<span>${esc(loc)}</span><span class="detail-meta-dot">·</span>` : ''}
        ${fr ? `<span>${esc(fr)}</span>` : ''}
        ${j.url ? `<a href="${esc(j.url)}" target="_blank" class="btn-sm" style="margin-left:4px;">View Posting ↗</a>` : ''}
      </div>
      <div class="review-banner">
        <div class="review-banner-title">Why it requires review</div>
        <div class="review-banner-text">${esc(j.eligibility_reason || 'Manual review required')}</div>
      </div>
      ${jdText ? `<div class="detail-section">
        <div class="detail-section-title detail-collapse-hdr" onclick="toggleCollapse('jd-${esc(j.job_id)}',this)">
          Job Details <span class="detail-collapse-icon">▼</span>
        </div>
        <div class="detail-collapse-body" id="jd-${esc(j.job_id)}">${esc(jdText)}</div>
      </div>` : ''}
    `;
    return;
  }

  // ELIGIBLE job: full detail A–I
  let execPkt = null;
  if (j.execution_packet) { try { execPkt = JSON.parse(j.execution_packet); } catch(e){} }

  // Section C: Fit breakdown
  const dims = [
    {key:'role_fit',label:'Role Fit'},{key:'experience_fit',label:'Exp Fit'},
    {key:'skill_fit',label:'Skill Fit'},{key:'portfolio_fit',label:'Portfolio'},
    {key:'domain_fit',label:'Domain Fit'},
  ];
  const hasFit = dims.some(d=>j[d.key]!=null);
  const fitHtml = hasFit ? `<div class="detail-section">
    <div class="detail-section-title">C. Fit Breakdown</div>
    ${dims.map(d=>j[d.key]!=null?`<div class="fit-row">
      <span class="fit-label">${d.label}</span>
      <div class="fit-track"><div class="fit-fill" style="width:${(j[d.key]/10)*100}%"></div></div>
      <span class="fit-score">${j[d.key]}</span>
    </div>`:'').join('')}
    ${j.overall_fit!=null?`<div style="margin-top:8px;font-size:12px;color:var(--text-muted);">Overall Fit: <span style="color:var(--lavender);font-weight:800;">${j.overall_fit}/10</span></div>`:''}
  </div>` : '';

  // Section D: Attack Plan
  let seqHtml = '';
  if (j.attack_action_sequence) {
    let seq = j.attack_action_sequence;
    if (typeof seq === 'string') { try { seq = JSON.parse(seq); } catch(e){ seq=[]; } }
    if (Array.isArray(seq) && seq.length) {
      seqHtml = seq.map((s,i)=>`${i>0?'<span class="seq-arrow">→</span>':''}<span class="seq-step"><span class="seq-num">${i+1}</span>${esc(s)}</span>`).join('');
    }
  }
  const attackHtml = (j.attack_priority||j.attack_intensity||j.attack_access_level||seqHtml) ? `<div class="detail-section">
    <div class="detail-section-title">D. Attack Plan</div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px;align-items:center;">
      ${j.attack_priority ? `<span class="p-chip ${p}">${esc(j.attack_priority)}</span>` : ''}
      ${j.attack_intensity ? `<span style="font-size:11px;color:var(--text-muted);">${esc(j.attack_intensity)}</span>` : ''}
      ${j.attack_access_level ? `<span class="access-badge access-${esc(j.attack_access_level)}">Access: ${esc(j.attack_access_level)}</span>` : ''}
    </div>
    ${seqHtml ? `<div class="action-seq">${seqHtml}</div>` : ''}
  </div>` : '';

  // Section E: Human Contact Data
  let contacts = [];
  // From execution packet
  if (execPkt && execPkt.steps) {
    for (const s of execPkt.steps) {
      if (s.type === 'linkedin' && (s.person_name || s.linkedin_url)) {
        contacts.push({name: s.person_name||'', role: s.person_role||'', linkedin_url: s.linkedin_url||''});
      }
    }
  }
  // From job fields (hiring manager)
  const hmContact = { name: j.hm_name||'', email: j.hm_email||'', linkedin_url: '' };

  let contactHtml = '';
  if (contacts.length || hmContact.name || hmContact.email || j.company_linkedin) {
    const blocks = [];
    if (contacts.length) {
      blocks.push(`<div style="font-size:11px;font-weight:800;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.4px;margin-bottom:6px;">Product Person</div>`
        + contacts.map(c=>`<div class="contact-block">
          ${c.name ? `<div class="contact-name">${esc(c.name)}</div>` : ''}
          ${c.role ? `<div class="contact-role">${esc(c.role)}</div>` : ''}
          <div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap;">
            ${c.linkedin_url ? `<a href="${esc(c.linkedin_url)}" target="_blank" class="btn-sm primary">Open LinkedIn ↗</a>` : ''}
          </div>
        </div>`).join(''));
    }
    if (hmContact.email) {
      blocks.push(`<div class="contact-block">
        ${hmContact.name ? `<div class="contact-name">${esc(hmContact.name)}</div>` : ''}
        <div class="contact-email">${esc(hmContact.email)}</div>
        <div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap;">
          <button class="btn-sm" onclick="copyText('${esc(hmContact.email)}',this,'Copy Email')">Copy Email</button>
          <a href="mailto:${esc(hmContact.email)}" class="btn-sm">Open Email ↗</a>
        </div>
      </div>`);
    }
    if (j.company_linkedin && !contacts.length) {
      blocks.push(`<div class="contact-block">
        <div class="contact-role">Company LinkedIn</div>
        <div style="margin-top:4px;"><a href="${esc(j.company_linkedin)}" target="_blank" class="btn-sm primary">Open LinkedIn ↗</a></div>
      </div>`);
    }
    if (blocks.length) {
      contactHtml = `<div class="detail-section"><div class="detail-section-title">E. Human Contact</div>${blocks.join('')}</div>`;
    }
  }

  // Sections F/G/H: Execution steps
  let execHtml = '';
  if (execPkt && execPkt.steps && execPkt.steps.length) {
    execHtml = `<div class="detail-section">
      <div class="detail-section-title">F–H. Execute</div>
      ${execPkt.steps.map(s=>renderExecStep(j.job_id,s,j)).join('')}
      <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap;">
        ${execPkt.steps.some(s=>s.type==='linkedin') ? `<button class="btn-sm primary" onclick="logAttackEvent('${esc(j.job_id)}','linkedin_sent',this)">Mark LinkedIn Sent</button>` : ''}
        ${execPkt.steps.some(s=>s.type==='email'||s.type==='email_unattributed') ? `<button class="btn-sm primary" onclick="logAttackEvent('${esc(j.job_id)}','email_sent',this)">Mark Email Sent</button>` : ''}
        ${execPkt.steps.some(s=>s.type==='apply') ? `<button class="btn-sm primary" onclick="logAttackEvent('${esc(j.job_id)}','applied',this)">Mark Applied</button>` : ''}
      </div>
    </div>`;
  } else {
    execHtml = `<div class="detail-section">
      <div class="detail-section-title">Execution Plan</div>
      <div class="empty" style="padding:12px 0;">No execution packet built yet.</div>
    </div>`;
  }

  // Section I: Job Details (collapsible)
  const jdText = j.description || j.text || '';
  const jdHtml = jdText ? `<div class="detail-section">
    <div class="detail-section-title">
      <span class="detail-collapse-hdr" onclick="toggleCollapse('jd-${esc(j.job_id)}',this)" style="display:flex;align-items:center;justify-content:space-between;cursor:pointer;width:100%;">
        I. Job Details <span class="detail-collapse-icon">▼</span>
      </span>
    </div>
    <div class="detail-collapse-body" id="jd-${esc(j.job_id)}">${esc(jdText)}</div>
  </div>` : '';

  document.getElementById('agent-detail').innerHTML = `
    <div class="detail-priority">
      <span class="p-chip ${p}">${esc(j.attack_priority||'')}</span>
      ${j.attack_intensity ? `<span style="font-size:11px;color:var(--text-dim);margin-left:6px;">${esc(j.attack_intensity)}</span>` : ''}
    </div>
    <div class="detail-title">${esc(j.title)}</div>
    <div class="detail-company">${esc(j.company)}</div>
    <div class="detail-meta-row">
      ${loc ? `<span>${esc(loc)}</span><span class="detail-meta-dot">·</span>` : ''}
      ${fr ? `<span>${esc(fr)}</span><span class="detail-meta-dot">·</span>` : ''}
      ${j.overall_fit != null ? `<span style="color:var(--lavender);font-weight:700;">Fit ${j.overall_fit}/10</span>` : ''}
      ${j.attack_access_level ? `<span class="access-badge access-${esc(j.attack_access_level)}">${esc(j.attack_access_level)}</span>` : ''}
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:16px;">
      ${j.url ? `<a href="${esc(j.url)}" target="_blank" class="btn-sm">View Posting ↗</a>` : ''}
      ${j.company_linkedin ? `<a href="${esc(j.company_linkedin)}" target="_blank" class="btn-sm">Company LinkedIn ↗</a>` : ''}
      ${j.hm_email ? `<a href="mailto:${esc(j.hm_email)}" class="btn-sm">${esc(j.hm_email)}</a>` : ''}
    </div>
    ${j.attack_reason||j.eligibility_reason ? `<div class="detail-section">
      <div class="detail-section-title">B. Why This Job</div>
      <div class="why-text">${esc(j.attack_reason||j.eligibility_reason)}</div>
    </div>` : ''}
    ${fitHtml}
    ${attackHtml}
    ${contactHtml}
    ${execHtml}
    ${jdHtml}
  `;
}

function toggleCollapse(id, hdr) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.toggle('open');
  const icon = hdr.querySelector('.detail-collapse-icon');
  if (icon) icon.style.transform = el.classList.contains('open') ? 'rotate(180deg)' : '';
}

// ---- Agent filter pills ----
document.querySelectorAll('.p-pill').forEach(btn => {
  btn.addEventListener('click', () => {
    agentFilter = btn.dataset.p;
    document.querySelectorAll('.p-pill').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    selectedJobId = null;
    renderAgentList();
    const hint = agentFilter === 'REVIEW'
      ? 'Select a job to see why it requires review.'
      : 'Select a job to see the full attack plan.';
    document.getElementById('agent-detail').innerHTML=`<div class="detail-empty">${hint}</div>`;
  });
});

async function logAttackEvent(jobId, eventType, btn) {
  let pk = kpiPasskey || getSavedPk();
  if (!pk) {
    pk = prompt('Enter passkey to log this action:');
    if (!pk) return;
    kpiPasskey = pk; savePk(pk);
  }
  const origText = btn.textContent;
  btn.disabled = true; btn.textContent = 'Logging…';
  try {
    const res = await fetch('/api/pipeline/log', {
      method:'POST',
      headers:{'Authorization':'Bearer '+pk,'Content-Type':'application/json'},
      body: JSON.stringify({job_id:jobId,event_type:eventType,action:'set',noted_at:new Date().toISOString()}),
    });
    if (res.status===401){btn.textContent='Wrong passkey';kpiPasskey='';btn.disabled=false;return;}
    if (res.ok){btn.textContent='Logged ✓';btn.style.color='var(--green)';kpiData=null;}
    else{const e=await res.json().catch(()=>({}));btn.textContent='Error: '+(e.error||res.status);}
  } catch(e){btn.textContent='Network error';}
  setTimeout(()=>{btn.disabled=false;btn.textContent=origText;btn.style.color='';},2500);
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
  const opps = (oppsData || []).filter(j => j.eligibility_status === 'ELIGIBLE');

  const stateMap = {};
  const followupDue = [];
  const inprogress = [];
  const responses = [];
  const recentlyCompleted = [];
  const sevenDaysAgo = Date.now() - 7 * 86400000;

  if (pipeState) {
    for (const j of pipeState.jobs) {
      stateMap[j.job_id] = j;
      if (j.followup_due) {
        followupDue.push(j);
      } else if (['RESPONDED','CONVERSATION','INTERVIEW','OFFER'].includes(j.state)) {
        responses.push(j);
      } else if (['WAITING','FOLLOW-UP DUE','APPLICATION SENT','NO RESPONSE'].includes(j.state)) {
        inprogress.push(j);
      }
      // Recently completed: any event in last 7 days
      if (j.events_at) {
        const recent = Object.values(j.events_at).some(ts => {
          try { return new Date(ts).getTime() >= sevenDaysAgo; } catch(e){ return false; }
        });
        if (recent && !['OFFER','INTERVIEW','CONVERSATION','RESPONDED'].includes(j.state)) {
          recentlyCompleted.push(j);
        }
      }
    }
  }

  const newActions = opps.filter(j => {
    if (!j.execution_packet) return false;
    if (!pipeState) return true;
    const s = stateMap[j.job_id];
    return !s || s.state === 'NOT STARTED';
  });

  let html = '';

  if (!pipeState) {
    html += `<div style="background:var(--lavender-dark);border:1px solid var(--lavender-border);border-radius:10px;padding:14px;margin-bottom:24px;font-size:13px;color:var(--lavender);">
      Unlock KPIs to see responses, follow-ups, and attacks in progress.
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
  if (recentlyCompleted.length) {
    html += `<div class="group-block"><div class="group-title completed">Recently Completed <span class="group-count completed">${recentlyCompleted.length}</span></div>
      ${recentlyCompleted.map(j=>actionCardHtml(j,null,'completed')).join('')}</div>`;
  }

  if (!html) {
    html = `<div class="catchup"><div class="catchup-icon">✓</div>You're all caught up.</div>`;
  }

  wrap.innerHTML = html;
  wrap.querySelectorAll('.action-card-hdr').forEach(hdr => {
    hdr.addEventListener('click', ()=>hdr.closest('.action-card').classList.toggle('open'));
  });
}

function actionCardHtml(pipeJob, oppJob, cls) {
  const j = oppJob || (oppsData && oppsData.find(o=>o.job_id===(pipeJob&&pipeJob.job_id))) || pipeJob;
  if (!j) return '';
  const title = j.title || (pipeJob&&pipeJob.title) || '';
  const company = j.company || (pipeJob&&pipeJob.company) || '';
  const jid = j.job_id;
  let bodyHtml = '';
  let execPkt = null;
  if (j.execution_packet) { try{execPkt=JSON.parse(j.execution_packet);}catch(e){} }

  if (execPkt && execPkt.steps) {
    bodyHtml = execPkt.steps.map(s=>renderExecStep(jid,s,j)).join('');
    // Per-type action buttons
    const hasLi = execPkt.steps.some(s=>s.type==='linkedin');
    const hasEm = execPkt.steps.some(s=>s.type==='email'||s.type==='email_unattributed');
    const hasAp = execPkt.steps.some(s=>s.type==='apply');
    bodyHtml += `<div style="display:flex;gap:6px;margin-top:10px;flex-wrap:wrap;">
      ${hasLi ? `<button class="btn-sm primary" onclick="logActionEvent('${esc(jid)}','linkedin_sent',this)">Mark LinkedIn Sent</button>` : ''}
      ${hasEm ? `<button class="btn-sm primary" onclick="logActionEvent('${esc(jid)}','email_sent',this)">Mark Email Sent</button>` : ''}
      ${hasAp ? `<button class="btn-sm primary" onclick="logActionEvent('${esc(jid)}','applied',this)">Mark Applied</button>` : ''}
    </div>`;
  } else if (pipeJob) {
    const sClass = STATE_CSS[pipeJob.state] || 's-not-started';
    bodyHtml = `<span class="state-badge ${sClass}">${esc(pipeJob.state)}</span>`;
  } else {
    bodyHtml = '<div class="empty" style="padding:10px 0;">No execution packet — run pipeline.</div>';
  }

  const stateLabel = pipeJob ? (pipeJob.state||'NOT STARTED') : '';
  const sClass = stateLabel ? (STATE_CSS[stateLabel]||'s-not-started') : '';

  return `<div class="action-card" id="ac-${jid}">
    <div class="action-card-hdr">
      <div class="action-title-wrap">
        <div class="action-title">${esc(title)}</div>
        <div class="action-company">${esc(company)}</div>
      </div>
      ${j.attack_priority ? `<span class="p-chip ${(j.attack_priority||'').toLowerCase()}">${esc(j.attack_priority)}</span>` : ''}
      ${stateLabel && stateLabel !== 'NOT STARTED' ? `<span class="state-badge ${sClass}" style="margin-left:4px;">${esc(stateLabel)}</span>` : ''}
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
const STATE_CSS = {
  'OFFER':'s-offer','INTERVIEW':'s-interview','CONVERSATION':'s-conversation',
  'RESPONDED':'s-responded','FOLLOW-UP DUE':'s-followup-due','WAITING':'s-waiting',
  'APPLICATION SENT':'s-app-sent','REJECTED':'s-rejected','NO RESPONSE':'s-no-response',
  'NOT STARTED':'s-not-started',
};

async function loadPipeline() {
  if (!summaryData) {
    try{const r=await fetch('/api/summary');summaryData=await r.json();}catch(e){document.getElementById('pipeline-funnel').innerHTML='<div class="empty">Could not load.</div>';return;}
  }
  const d = summaryData;

  // Stage counts from summary
  const rejected = (d.discovered||0) - (d.eligible||0) - (d.review||0);

  // Later stages from kpiData if available
  const pk = kpiPasskey || getSavedPk();
  if (pk && !kpiData) {
    try {
      const r = await fetch('/api/pipeline/state',{headers:{'Authorization':'Bearer '+pk}});
      if (r.ok) { kpiData = await r.json(); kpiPasskey = pk; }
    } catch(e){}
  }

  let attacked=0,waiting=0,responded=0,conversation=0,interview=0,offer=0;
  if (kpiData) {
    for (const j of kpiData.jobs) {
      if (j.applied||j.linkedin_sent||j.email_sent) attacked++;
      if (j.state==='WAITING'||j.state==='APPLICATION SENT') waiting++;
      if (j.state==='RESPONDED') responded++;
      if (j.state==='CONVERSATION') conversation++;
      if (j.state==='INTERVIEW') interview++;
      if (j.state==='OFFER') offer++;
    }
  }

  const mainStages = [
    {key:'discovered',v:d.discovered,l:'Discovered',prev:null},
    {key:'eligible',v:d.eligible,l:'Eligible',prev:d.discovered},
    {key:'prioritized',v:d.prioritized,l:'Prioritized',prev:d.eligible},
    {key:'attacked',v:kpiData?attacked:null,l:'Attacked',prev:d.prioritized},
    {key:'waiting',v:kpiData?waiting:null,l:'Waiting',prev:kpiData?attacked:null},
    {key:'responded',v:kpiData?responded:null,l:'Responded',prev:kpiData?waiting:null},
    {key:'conversation',v:kpiData?conversation:null,l:'Conversation',prev:kpiData?responded:null},
    {key:'interview',v:kpiData?interview:null,l:'Interview',prev:kpiData?conversation:null},
    {key:'offer',v:kpiData?offer:null,l:'Offer',prev:kpiData?interview:null},
  ];

  const funnelHtml = mainStages.map((s,i) => {
    const rate = (s.v!=null && s.prev) ? Math.round((s.v/s.prev)*100)+'%' : '';
    const locked = s.v === null;
    return `<div class="funnel-stage${pipeStageOpen===s.key?' active':''}" onclick="openPipeStage('${s.key}')">
      <div class="funnel-sval">${locked?'<span style="font-size:14px;color:var(--text-dim);">🔒</span>':((s.v??'–'))}</div>
      <div class="funnel-slbl">${s.l}</div>
      ${rate?`<div class="funnel-srate">${rate}</div>`:''}
    </div>`;
  }).join('');

  const branchHtml = `
    <div class="branch-card review${pipeStageOpen==='review'?' active':''}" onclick="openPipeStage('review')">
      <div class="branch-val">${d.review??'–'}</div>
      <div class="branch-lbl">Review</div>
    </div>
    <div class="branch-card rejected${pipeStageOpen==='rejected'?' active':''}" onclick="openPipeStage('rejected')">
      <div class="branch-val">${rejected>=0?rejected:'–'}</div>
      <div class="branch-lbl">Rejected</div>
    </div>`;

  document.getElementById('pipeline-funnel').innerHTML = `
    <div class="funnel-full">
      <div style="font-size:11px;font-weight:800;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;">Main Funnel — click a stage to see jobs</div>
      <div class="funnel-row">${funnelHtml}</div>
      <div style="font-size:11px;font-weight:800;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.5px;margin:12px 0 8px;">Branches</div>
      <div class="funnel-branches">${branchHtml}</div>
      ${!kpiData?`<div style="font-size:12px;color:var(--text-dim);margin-bottom:12px;">🔒 Attacked → Offer stages require passkey — unlock KPIs first.</div>`:''}
      <div id="pipe-stage-detail"></div>
    </div>`;

  if (pipeStageOpen) renderPipeStageDetail(pipeStageOpen);

  const srcs = d.sources||{};
  const srcLabels = {hackernews:'Hacker News',cutshort:'Cutshort',iimjobs:'iimjobs',google_jobs:'Google Jobs',internshala:'Internshala',jsearch:'JSearch',yc:'YC Jobs',careers:'Direct Careers'};
  const srcEntries = Object.entries(srcs).sort((a,b)=>b[1]-a[1]);
  document.getElementById('pipeline-sources').innerHTML = srcEntries.length
    ? srcEntries.map(([src,cnt])=>`<tr><td class="name">${esc(srcLabels[src]||src)}</td><td class="num">${cnt}</td></tr>`).join('')
    : '<tr><td colspan="2" class="empty" style="text-align:center;">No eligible jobs yet.</td></tr>';
}

function openPipeStage(key) {
  pipeStageOpen = pipeStageOpen === key ? null : key;
  loadPipeline();
}

function renderPipeStageDetail(key) {
  const el = document.getElementById('pipe-stage-detail');
  if (!el) return;

  let jobs = [];
  let title = key.toUpperCase();

  if (key === 'discovered') {
    el.innerHTML = `<div class="stage-jobs-panel"><div class="stage-jobs-hdr"><span class="stage-jobs-title">All Discovered (${summaryData.discovered})</span></div><div class="empty" style="padding:8px 0;font-size:12px;">All scraped jobs — ${summaryData.eligible} eligible · ${summaryData.review} review · ${((summaryData.discovered||0)-(summaryData.eligible||0)-(summaryData.review||0))} rejected</div></div>`;
    return;
  }
  if (key === 'rejected') {
    el.innerHTML = `<div class="stage-jobs-panel"><div class="stage-jobs-hdr"><span class="stage-jobs-title" style="color:var(--red);">Rejected (${Math.max(0,(summaryData.discovered||0)-(summaryData.eligible||0)-(summaryData.review||0))})</span></div><div class="empty" style="padding:8px 0;font-size:12px;">Rejected jobs are not available for display.</div></div>`;
    return;
  }
  if ((key==='attacked'||key==='waiting'||key==='responded'||key==='conversation'||key==='interview'||key==='offer') && !kpiData) {
    el.innerHTML = `<div class="stage-jobs-panel"><div class="empty" style="padding:8px 0;font-size:12px;">🔒 Unlock KPIs to see jobs at this stage.</div></div>`;
    return;
  }

  const opp = oppsData || [];
  if (key === 'eligible') {
    jobs = opp.filter(j=>j.eligibility_status==='ELIGIBLE');
  } else if (key === 'review') {
    jobs = opp.filter(j=>j.eligibility_status==='REVIEW');
  } else if (key === 'prioritized') {
    jobs = opp.filter(j=>j.attack_priority&&j.eligibility_status==='ELIGIBLE');
  } else if (kpiData) {
    const stateFilter = {
      attacked: j => j.applied||j.linkedin_sent||j.email_sent,
      waiting: j => j.state==='WAITING'||j.state==='APPLICATION SENT',
      responded: j => j.state==='RESPONDED',
      conversation: j => j.state==='CONVERSATION',
      interview: j => j.state==='INTERVIEW',
      offer: j => j.state==='OFFER',
    }[key];
    const kpiJobs = stateFilter ? kpiData.jobs.filter(stateFilter) : [];
    jobs = kpiJobs.map(kj => {
      const oJob = opp.find(o=>o.job_id===kj.job_id);
      return oJob ? {...oJob, ...kj} : kj;
    });
  }

  if (!jobs.length) {
    el.innerHTML = `<div class="stage-jobs-panel"><div class="empty" style="padding:8px 0;font-size:12px;">No jobs at this stage yet.</div></div>`;
    return;
  }

  el.innerHTML = `<div class="stage-jobs-panel">
    <div class="stage-jobs-hdr"><span class="stage-jobs-title">${esc(title)} (${jobs.length})</span></div>
    ${jobs.slice(0,30).map(j=>`<div class="stage-job-row">
      <div style="flex:1;min-width:0;">
        <div class="stage-job-title">${esc(j.title||'')}</div>
        <div class="stage-job-co">${esc(j.company||j.author||'')}</div>
      </div>
      ${j.attack_priority?`<span class="p-chip ${j.attack_priority.toLowerCase()} stage-job-chip">${esc(j.attack_priority)}</span>`:''}
      ${j.state&&j.state!=='NOT STARTED'?`<span class="state-badge ${STATE_CSS[j.state]||'s-not-started'}">${esc(j.state)}</span>`:''}
    </div>`).join('')}
    ${jobs.length>30?`<div style="font-size:12px;color:var(--text-dim);padding:8px 0;">+ ${jobs.length-30} more</div>`:''}
  </div>`;
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
    renderKpiAttackExec(kpiData.jobs);
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

function renderKpiAttackExec(jobs) {
  const opps = oppsData || [];
  const eligible = opps.filter(j=>j.eligibility_status==='ELIGIBLE');
  const withPacket = eligible.filter(j=>j.execution_packet).length;
  const acted = jobs.filter(j=>j.applied||j.linkedin_sent||j.email_sent).length;
  const pct = withPacket > 0 ? Math.round((acted/withPacket)*100) : 0;

  const byP = {P1:{total:0,acted:0},P2:{total:0,acted:0},P3:{total:0,acted:0}};
  for (const opp of eligible) {
    const p = opp.attack_priority;
    if (!p || !byP[p]) continue;
    byP[p].total++;
    const kj = jobs.find(j=>j.job_id===opp.job_id);
    if (kj && (kj.applied||kj.linkedin_sent||kj.email_sent)) byP[p].acted++;
  }
  const p1pct = byP.P1.total ? Math.round(byP.P1.acted/byP.P1.total*100) : 0;
  const p2pct = byP.P2.total ? Math.round(byP.P2.acted/byP.P2.total*100) : 0;
  const p3pct = byP.P3.total ? Math.round(byP.P3.acted/byP.P3.total*100) : 0;

  const cards = [
    {v:withPacket,l:'Actions Ready',cls:'lav'},
    {v:acted,l:'Completed',cls:'green'},
    {v:pct+'%',l:'Completion',cls:'pink'},
    {v:p1pct+'%',l:'P1 Execution',cls:'pink'},
    {v:p2pct+'%',l:'P2 Execution',cls:'lav'},
    {v:p3pct+'%',l:'P3 Execution',cls:'dim'},
  ];
  document.getElementById('kpi-attack-exec').innerHTML=cards.map(c=>`<div class="attack-exec-card ${c.cls}"><div class="attack-exec-val">${c.v}</div><div class="attack-exec-lbl">${c.l}</div></div>`).join('');
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
<div class="tab-footer-note">✦ New version coming — designs in iteration, launching this week</div>
</body>
</html>

"""


if __name__ == "__main__":
    database.init_db()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
