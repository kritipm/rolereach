import os
from datetime import datetime, timedelta

from flask import Flask, jsonify, request

import config
import database
import telegram_bot

app = Flask(__name__)

STATUS_CYCLE = ["NEW", "Sent", "Replied", "Interview", "Skip"]
SOURCES = ["hackernews", "cutshort", "iimjobs", "google_jobs", "internshala"]
SOURCE_LABELS = {
    "hackernews": "Hacker News",
    "cutshort": "Cutshort",
    "iimjobs": "iimjobs",
    "google_jobs": "Google Jobs",
    "internshala": "Internshala",
}
WEEKLY_GOAL_TARGET = 10


# ---------- Shared helpers ----------


def get_all_jobs_with_meta():
    database.init_db()
    with database.get_connection() as conn:
        jobs = [dict(r) for r in conn.execute("SELECT * FROM jobs").fetchall()]

    actions = database.fetch_all_job_actions()

    for job in jobs:
        job["title"] = telegram_bot.get_title(job)
        job["location"] = telegram_bot.get_location(job)
        job["experience"] = telegram_bot.get_experience(job)
        job["tier"] = telegram_bot.get_job_tier(job)
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

    return jsonify(
        {
            "jobs_count": len(jobs),
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
                "job_id": j["comment_id"],
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
            }
            for j in jobs
        ]
    )


@app.route("/api/action", methods=["POST"])
def api_action():
    payload = request.get_json(force=True, silent=True) or {}
    job_id = payload.get("job_id")
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


DASHBOARD_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RoleReach</title>
<style>
  :root {
    --bg: #09081C;
    --card: #100F2A;
    --card-hover: #171540;
    --border: #1A183C;
    --pink: #C830F0;
    --pink-glow: rgba(200,48,240,0.4);
    --pink-dark: #180828;
    --pink-border: #2C0A42;
    --purple: #8060C0;
    --lavender: #DDB0FF;
    --lavender-dark: #140C2C;
    --lavender-border: #201848;
    --text-primary: #FFFFFF;
    --text-secondary: #EFEFEF;
    --text-muted: #B0B0B0;
    --text-dim: #505060;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text-primary);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    min-height: 100vh;
  }
  .mono { font-family: "SF Mono", "Cascadia Code", Consolas, monospace; }

  header { padding: 30px 36px 0 36px; }
  h1 {
    margin: 0 0 4px 0;
    font-size: 24px;
    font-weight: 800;
    letter-spacing: 0.4px;
    background: linear-gradient(90deg, #FFFFFF, var(--pink));
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    display: inline-block;
  }
  .tabs { display: flex; gap: 4px; margin: 22px 0 0 0; border-bottom: 1px solid var(--border); padding: 0 36px; }
  .tab-btn {
    background: none; border: none; color: var(--text-muted); font-size: 14.5px; font-weight: 700;
    padding: 12px 18px; cursor: pointer; border-bottom: 3px solid transparent;
    transition: color 0.15s ease, border-color 0.15s ease;
  }
  .tab-btn:hover { color: var(--text-primary); }
  .tab-btn.active { color: var(--pink); border-bottom-color: var(--pink); }

  main { padding: 30px 36px 70px 36px; max-width: 1320px; margin: 0 auto; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  /* ---------- AGENT TAB ---------- */
  .hero-row { display: flex; align-items: baseline; gap: 18px; margin-bottom: 20px; flex-wrap: wrap; }
  .hero-number { font-size: 64px; font-weight: 800; line-height: 1;
    background: linear-gradient(90deg, #FFFFFF, var(--pink));
    -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
  }
  .hero-caption { font-size: 16px; font-weight: 700; color: var(--text-primary); }

  .chip-row { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 28px; }
  .chip {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 8px 16px; border-radius: 999px; font-size: 13px; font-weight: 700;
  }
  .chip-pink { background: var(--pink-dark); border: 1px solid var(--pink-border); color: var(--pink); box-shadow: 0 0 16px var(--pink-glow); }
  .chip-neutral { background: var(--card); border: 1px solid var(--border); color: var(--text-muted); }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--pink); display: inline-block; }

  .tier-row { display: flex; gap: 16px; margin-bottom: 28px; flex-wrap: wrap; }
  .tier-card {
    flex: 1 1 260px; background: var(--card); border: 1px solid var(--border); border-radius: 16px;
    padding: 20px 22px; display: flex; align-items: center; gap: 16px;
  }
  .tier-circle {
    width: 46px; height: 46px; border-radius: 50%; flex-shrink: 0;
  }
  .tier-circle.magenta { background: radial-gradient(circle at 35% 30%, #F0A0FF, var(--pink)); box-shadow: 0 0 20px var(--pink-glow); }
  .tier-circle.lavender { background: radial-gradient(circle at 35% 30%, #F0E0FF, var(--lavender)); box-shadow: 0 0 20px rgba(221,176,255,0.35); }
  .tier-count { font-size: 30px; font-weight: 800; color: var(--text-primary); }
  .tier-label { font-size: 13px; font-weight: 700; color: var(--lavender); text-transform: uppercase; letter-spacing: 0.4px; }

  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  @media (max-width: 880px) { .two-col { grid-template-columns: 1fr; } }
  .panel { background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 22px 24px; }
  .panel-title { font-size: 13px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); margin-bottom: 18px; }

  .source-line { margin-bottom: 16px; }
  .source-line-top { display: flex; justify-content: space-between; margin-bottom: 6px; font-size: 14px; }
  .source-line-top .name { color: var(--text-primary); font-weight: 600; }
  .source-line-top .count { color: var(--text-primary); font-weight: 700; }
  .source-bar-track { height: 6px; border-radius: 4px; background: rgba(255,255,255,0.05); overflow: hidden; }
  .source-bar-fill {
    height: 100%; border-radius: 4px; background: linear-gradient(90deg, var(--purple), var(--pink));
    width: 0%; animation: growBar 0.9s ease forwards;
  }
  @keyframes growBar { to { width: var(--target-width); } }

  .donut-wrap { display: flex; align-items: center; gap: 24px; }
  .donut {
    width: 130px; height: 130px; border-radius: 50%; flex-shrink: 0;
    background: conic-gradient(var(--pink) 0deg 0deg, var(--purple) 0deg 0deg, var(--text-dim) 0deg 360deg);
    position: relative;
  }
  .donut::after {
    content: ""; position: absolute; inset: 18px; border-radius: 50%; background: var(--card);
  }
  .legend-item { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-secondary); margin-bottom: 8px; }
  .legend-swatch { width: 10px; height: 10px; border-radius: 3px; }
  .divider { height: 1px; background: var(--border); margin: 18px 0; }
  .drafts-big { font-size: 34px; font-weight: 800; color: var(--pink); }
  .drafts-small { font-size: 13px; color: var(--text-muted); }

  /* ---------- ACTIONS TAB ---------- */
  .summary-strip { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 22px; }
  .summary-card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 16px 18px; }
  .summary-card .label { font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.4px; margin-bottom: 8px; }
  .summary-card .value { font-size: 26px; font-weight: 800; }
  .summary-card.total .value { color: var(--text-primary); }
  .summary-card.sent .value { color: var(--pink); }
  .summary-card.replied .value { color: var(--purple); }
  .summary-card.interview .value { color: var(--lavender); }

  .filter-row { display: flex; gap: 8px; margin-bottom: 24px; flex-wrap: wrap; }
  .filter-pill {
    background: var(--card); border: 1px solid var(--border); color: var(--text-muted);
    padding: 7px 16px; border-radius: 999px; font-size: 13px; font-weight: 700; cursor: pointer;
  }
  .filter-pill:hover { color: var(--text-primary); }
  .filter-pill.active { background: var(--pink-dark); border-color: var(--pink); color: var(--pink); box-shadow: 0 0 14px var(--pink-glow); }

  .section-block { margin-bottom: 30px; }
  .section-label {
    display: flex; align-items: center; gap: 12px; margin-bottom: 14px; padding-left: 12px;
    border-left: 3px solid var(--text-dim);
  }
  .section-label.act-now { border-left-color: var(--pink); }
  .section-label.review { border-left-color: var(--lavender); }
  .section-title { font-size: 13px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-primary); }
  .section-meta { font-size: 12.5px; color: var(--text-muted); }
  .section-count-badge {
    margin-left: auto; font-size: 12px; font-weight: 800; padding: 3px 10px; border-radius: 999px;
    background: var(--card); color: var(--text-muted);
  }
  .section-count-badge.act-now { background: var(--pink-dark); color: var(--pink); }
  .section-count-badge.review { background: var(--lavender-dark); color: var(--lavender); }

  .job-row {
    background: var(--card); border: 1px solid var(--border); border-left: 3px solid transparent;
    border-radius: 12px; padding: 14px 18px; margin-bottom: 10px; cursor: pointer;
    transition: background 0.15s ease, border-color 0.15s ease;
  }
  .job-row:hover { background: var(--card-hover); }
  .job-row.status-sent { border-left-color: var(--pink); background: rgba(200,48,240,0.05); }
  .job-row.status-replied { border-left-color: var(--purple); background: rgba(128,96,192,0.07); }
  .job-row.status-interview { border-left-color: var(--lavender); background: rgba(221,176,255,0.06); }
  .job-row.status-skip { border-left-color: var(--text-dim); opacity: 0.55; }

  .job-top { display: flex; align-items: center; gap: 10px; }
  .job-title { font-size: 15px; font-weight: 700; color: var(--text-primary); flex: 1; }
  .status-pill {
    display: inline-flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 800;
    padding: 4px 10px; border-radius: 999px; border: 1px solid rgba(255,255,255,0.15);
    background: rgba(255,255,255,0.06); color: var(--text-primary); cursor: pointer; white-space: nowrap;
  }
  .status-pill.sent { background: var(--pink-dark); border-color: var(--pink-border); color: var(--pink); }
  .status-pill.replied { background: rgba(128,96,192,0.18); border-color: var(--purple); color: var(--lavender); }
  .status-pill.interview { background: var(--lavender-dark); border-color: var(--lavender-border); color: var(--lavender); }
  .status-pill.skip { background: rgba(80,80,96,0.2); border-color: var(--text-dim); color: var(--text-muted); }
  .pulse-dot {
    width: 6px; height: 6px; border-radius: 50%; background: var(--pink); display: inline-block;
    animation: pulse 1.4s infinite ease-in-out;
  }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }

  .exp-badge { font-size: 11px; font-weight: 800; padding: 4px 10px; border-radius: 999px; white-space: nowrap; }
  .exp-badge.tier1 { background: var(--pink-dark); border: 1px solid var(--pink-border); color: var(--pink); box-shadow: 0 0 10px var(--pink-glow); }
  .exp-badge.tier2 { background: var(--lavender-dark); border: 1px solid var(--lavender-border); color: var(--lavender); }

  .job-bottom { display: flex; align-items: center; gap: 12px; margin-top: 8px; padding-left: 2px; flex-wrap: wrap; }
  .job-company { font-weight: 700; color: var(--text-primary); font-size: 13.5px; }
  .location-pill {
    display: inline-flex; align-items: center; gap: 5px; font-size: 12px; font-weight: 600;
    background: var(--pink-dark); border: 1px solid var(--pink-border); color: var(--pink);
    padding: 3px 10px; border-radius: 999px; box-shadow: 0 0 10px var(--pink-glow);
  }
  .job-bottom-right { margin-left: auto; }
  .contact-chip {
    display: inline-flex; align-items: center; gap: 5px; font-size: 12px; font-weight: 700;
    padding: 4px 11px; border-radius: 999px;
  }
  .contact-chip.email { background: var(--pink-dark); border: 1px solid var(--pink-border); color: var(--pink); box-shadow: 0 0 10px var(--pink-glow); }
  .contact-chip.linkedin { background: var(--lavender-dark); border: 1px solid var(--lavender-border); color: var(--lavender); }
  .contact-chip.none { background: rgba(80,80,96,0.18); color: var(--text-dim); }

  .draft-panel {
    max-height: 0; overflow: hidden; transition: max-height 0.3s ease;
  }
  .draft-panel.open { max-height: 400px; margin-top: 12px; }
  .draft-box {
    background: rgba(0,0,0,0.3); border: 1px solid var(--border); border-radius: 10px;
    padding: 12px 14px; font-size: 12.5px; line-height: 1.55; white-space: pre-wrap;
    color: var(--text-secondary); max-height: 260px; overflow-y: auto; margin-bottom: 8px;
  }
  .copy-btn {
    background: var(--pink-dark); border: 1px solid var(--pink-border); color: var(--pink);
    font-size: 11.5px; font-weight: 700; padding: 6px 14px; border-radius: 8px; cursor: pointer;
  }
  .copy-btn.copied { background: var(--purple); color: #fff; border-color: var(--purple); }

  .empty-note, .loading-note { color: var(--text-muted); padding: 30px 0; text-align: center; }

  /* ---------- PIPELINE TAB ---------- */
  .ring-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 28px; }
  @media (max-width: 880px) { .ring-row { grid-template-columns: repeat(2, 1fr); } }
  .ring-card { background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 20px; text-align: center; }
  .ring-svg-wrap { position: relative; width: 120px; height: 120px; margin: 0 auto 12px auto; }
  .ring-number { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 26px; font-weight: 800; }
  .ring-label { font-size: 12px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.4px; color: var(--text-primary); }
  .ring-desc { font-size: 11.5px; color: var(--text-muted); margin-top: 4px; }

  .goal-card { background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 22px 24px; margin-bottom: 28px; }
  .goal-top { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 12px; }
  .goal-label { font-size: 13px; font-weight: 800; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.4px; }
  .goal-counter { font-size: 22px; font-weight: 800; color: var(--pink); }
  .goal-track { height: 12px; border-radius: 8px; background: rgba(255,255,255,0.06); overflow: hidden; }
  .goal-fill {
    height: 100%; border-radius: 8px; background: linear-gradient(90deg, var(--purple), var(--pink));
    box-shadow: 0 0 14px var(--pink-glow); transition: width 0.5s ease;
  }

  .perf-table { width: 100%; border-collapse: collapse; }
  .perf-table th { text-align: left; font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.4px; padding: 10px 12px; border-bottom: 1px solid var(--border); }
  .perf-table td { padding: 12px; border-bottom: 1px solid var(--border); font-size: 14px; }
  .perf-table td.name { color: var(--text-primary); font-weight: 700; }
  .perf-table td.num { font-family: "SF Mono", Consolas, monospace; color: var(--text-secondary); }
</style>
</head>
<body>

<header>
  <h1>RoleReach</h1>
</header>

<div class="tabs">
  <button class="tab-btn active" data-tab="agent">Agent</button>
  <button class="tab-btn" data-tab="actions">Actions</button>
  <button class="tab-btn" data-tab="pipeline">Pipeline</button>
</div>

<main>

  <section id="tab-agent" class="tab-content active">
    <div id="agent-hero"></div>
    <div class="chip-row" id="agent-chips"></div>
    <div class="tier-row" id="agent-tiers"></div>
    <div class="two-col">
      <div class="panel">
        <div class="panel-title">Jobs by source</div>
        <div id="agent-sources"></div>
      </div>
      <div class="panel">
        <div class="panel-title">Contact resolution</div>
        <div id="agent-donut"></div>
        <div class="divider"></div>
        <div id="agent-drafts"></div>
      </div>
    </div>
  </section>

  <section id="tab-actions" class="tab-content">
    <div class="summary-strip" id="actions-summary"></div>
    <div class="filter-row" id="filter-row"></div>
    <div id="job-sections"><div class="loading-note">Loading jobs…</div></div>
  </section>

  <section id="tab-pipeline" class="tab-content">
    <div class="ring-row" id="pipeline-rings"></div>
    <div class="goal-card" id="pipeline-goal"></div>
    <div class="panel">
      <div class="panel-title">Source performance</div>
      <table class="perf-table">
        <thead><tr><th>Source</th><th>Seen</th><th>Sent</th><th>Replies</th></tr></thead>
        <tbody id="perf-tbody"></tbody>
      </table>
    </div>
  </section>

</main>

<script>
const FILTERS = ["All", "Sent", "Replied", "Interview", "Skip"];
let currentFilter = "All";

const STATUS_ICON = { "NEW": "&#9679;", "Sent": "&#10003;", "Replied": "&#8617;", "Interview": "&#9733;", "Skip": "&#10005;" };
const STATUS_NEXT = { "NEW": "Sent", "Sent": "Replied", "Replied": "Interview", "Interview": "Skip", "Skip": "NEW" };

function escapeHtml(str) {
  if (!str) return "";
  return str.replace(/[&<>"']/g, m => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[m]));
}

function switchTab(name) {
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab-content").forEach(s => s.classList.toggle("active", s.id === "tab-" + name));
  if (name === "agent") loadAgent();
  if (name === "actions") loadJobs();
  if (name === "pipeline") loadPipeline();
}
document.querySelectorAll(".tab-btn").forEach(btn => btn.addEventListener("click", () => switchTab(btn.dataset.tab)));

// ---------- AGENT ----------

async function loadAgent() {
  const res = await fetch("/api/agent");
  const d = await res.json();

  document.getElementById("agent-hero").innerHTML = `
    <div class="hero-row">
      <div class="hero-number mono">${d.jobs_count}</div>
      <div class="hero-caption">PM roles found today</div>
    </div>`;

  document.getElementById("agent-chips").innerHTML = `
    <span class="chip chip-pink"><span class="dot"></span>${d.drafts_ready} cold email drafts ready</span>
    <span class="chip chip-neutral">Runs daily at ${d.runs_daily_at}</span>`;

  document.getElementById("agent-tiers").innerHTML = `
    <div class="tier-card">
      <div class="tier-circle magenta"></div>
      <div><div class="tier-count">${d.tier1.count}</div><div class="tier-label">${d.tier1.label}</div></div>
    </div>
    <div class="tier-card">
      <div class="tier-circle lavender"></div>
      <div><div class="tier-count">${d.tier2.count}</div><div class="tier-label">${d.tier2.label}</div></div>
    </div>`;

  const maxCount = Math.max(1, ...d.sources.map(s => s.count));
  document.getElementById("agent-sources").innerHTML = d.sources.map((s, i) => `
    <div class="source-line">
      <div class="source-line-top"><span class="name">${escapeHtml(s.name)}</span><span class="count mono">${s.count}</span></div>
      <div class="source-bar-track"><div class="source-bar-fill" style="--target-width:${Math.round(100*s.count/maxCount)}%; animation-delay:${i*0.1}s"></div></div>
    </div>`).join("");

  const c = d.contact;
  const total = Math.max(1, c.named_email + c.linkedin_only + c.no_contact);
  const p1 = 360 * c.named_email / total;
  const p2 = p1 + 360 * c.linkedin_only / total;
  document.getElementById("agent-donut").innerHTML = `
    <div class="donut-wrap">
      <div class="donut" style="background: conic-gradient(var(--pink) 0deg ${p1}deg, var(--purple) ${p1}deg ${p2}deg, var(--text-dim) ${p2}deg 360deg);"></div>
      <div>
        <div class="legend-item"><span class="legend-swatch" style="background:var(--pink)"></span>Named email &mdash; ${c.named_email}</div>
        <div class="legend-item"><span class="legend-swatch" style="background:var(--purple)"></span>LinkedIn only &mdash; ${c.linkedin_only}</div>
        <div class="legend-item"><span class="legend-swatch" style="background:var(--text-dim)"></span>No contact &mdash; ${c.no_contact}</div>
      </div>
    </div>`;

  document.getElementById("agent-drafts").innerHTML = `
    <div class="drafts-big">${d.drafts_ready}</div>
    <div class="drafts-small">cold email drafts ready in your voice</div>`;
}

// ---------- ACTIONS ----------

function renderFilterRow() {
  document.getElementById("filter-row").innerHTML = FILTERS.map(f =>
    `<button class="filter-pill ${f === currentFilter ? 'active' : ''}" data-filter="${f}">${f}</button>`
  ).join("");
  document.querySelectorAll(".filter-pill").forEach(p => {
    p.addEventListener("click", () => { currentFilter = p.dataset.filter; renderFilterRow(); loadJobs(); });
  });
}

function jobRowHtml(job) {
  const statusKey = job.status.toLowerCase();
  const expBadge = job.tier === 1
    ? '<span class="exp-badge tier1">0-1yr / Fresher</span>'
    : '<span class="exp-badge tier2">1-3yr</span>';

  const statusPillClass = statusKey === "new" ? "" : statusKey;
  const pillContent = job.status === "NEW"
    ? `<span class="pulse-dot"></span>NEW`
    : `${STATUS_ICON[job.status]} ${job.status}`;

  let contactChip = '<span class="contact-chip none">No contact</span>';
  if (job.hm_email) {
    contactChip = `<span class="contact-chip email">&#9993; ${escapeHtml(job.hm_email)}</span>`;
  } else if (job.company_linkedin) {
    contactChip = `<span class="contact-chip linkedin">&#8599; LinkedIn</span>`;
  }

  const draftHtml = job.email_draft
    ? `<div class="draft-panel" id="draft-panel-${job.job_id}">
         <div class="draft-box" id="draft-${job.job_id}">${escapeHtml(job.email_draft)}</div>
         <button class="copy-btn" onclick="event.stopPropagation(); copyDraft(${job.job_id})">Copy draft</button>
       </div>`
    : "";

  return `<div class="job-row status-${statusKey}" id="job-${job.job_id}" onclick="toggleDraft(${job.job_id})">
    <div class="job-top">
      <span class="status-pill ${statusPillClass}" onclick="event.stopPropagation(); cycleStatus(${job.job_id}, '${job.status}')">${pillContent}</span>
      <span class="job-title">${escapeHtml(job.title)}</span>
      ${expBadge}
    </div>
    <div class="job-bottom">
      <span class="job-company">${escapeHtml(job.company)}</span>
      <span class="location-pill">&#128205; ${escapeHtml(job.location)}</span>
      <span class="job-bottom-right">${contactChip}</span>
    </div>
    ${draftHtml}
  </div>`;
}

function sectionHtml(key, title, meta, jobs, cls) {
  if (jobs.length === 0) return "";
  return `<div class="section-block">
    <div class="section-label ${cls}">
      <span class="section-title">${title}</span>
      <span class="section-meta">${meta}</span>
      <span class="section-count-badge ${cls}">${jobs.length}</span>
    </div>
    ${jobs.map(jobRowHtml).join("")}
  </div>`;
}

async function loadJobs() {
  const container = document.getElementById("job-sections");
  container.innerHTML = '<div class="loading-note">Loading jobs…</div>';

  const [jobsRes, agentRes] = await Promise.all([
    fetch("/api/jobs?status=" + encodeURIComponent(currentFilter)),
    fetch("/api/agent"),
  ]);
  const jobs = await jobsRes.json();

  const actRes = await fetch("/api/jobs?status=All");
  const allJobs = await actRes.json();
  const total = allJobs.length;
  const sent = allJobs.filter(j => ["Sent","Replied","Interview"].includes(j.status)).length;
  const replied = allJobs.filter(j => ["Replied","Interview"].includes(j.status)).length;
  const interview = allJobs.filter(j => j.status === "Interview").length;

  document.getElementById("actions-summary").innerHTML = `
    <div class="summary-card total"><div class="label">Total</div><div class="value">${total}</div></div>
    <div class="summary-card sent"><div class="label">Sent</div><div class="value">${sent}</div></div>
    <div class="summary-card replied"><div class="label">Replied</div><div class="value">${replied}</div></div>
    <div class="summary-card interview"><div class="label">Interview</div><div class="value">${interview}</div></div>`;

  if (jobs.length === 0) {
    container.innerHTML = '<div class="empty-note">No jobs match this filter.</div>';
    return;
  }

  const actNow = jobs.filter(j => j.group === "act_now");
  const review = jobs.filter(j => j.group === "review");
  const noContact = jobs.filter(j => j.group === "no_contact");

  container.innerHTML =
    sectionHtml("act_now", "Act now", "Has a named email &mdash; send today", actNow, "act-now") +
    sectionHtml("review", "Review", "LinkedIn found, no direct email yet", review, "review") +
    sectionHtml("no_contact", "No contact", "Nothing found yet &mdash; low priority", noContact, "");
}

async function cycleStatus(jobId, currentStatus) {
  const nextStatus = STATUS_NEXT[currentStatus] || "NEW";
  const res = await fetch("/api/action", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({job_id: jobId, status: nextStatus, timestamp: new Date().toISOString()}),
  });
  if (!res.ok) return;
  loadJobs();
}

function toggleDraft(jobId) {
  const panel = document.getElementById("draft-panel-" + jobId);
  if (!panel) return;
  panel.classList.toggle("open");
}

function copyDraft(jobId) {
  const text = document.getElementById("draft-" + jobId).innerText;
  navigator.clipboard.writeText(text).then(() => {
    const btn = event.target;
    btn.textContent = "Copied!";
    btn.classList.add("copied");
    setTimeout(() => { btn.textContent = "Copy draft"; btn.classList.remove("copied"); }, 1500);
  });
}

// ---------- PIPELINE ----------

function ringSvg(pct, glow) {
  const r = 50, c = 2 * Math.PI * r;
  const dash = c * Math.min(1, pct);
  const strokeColor = glow ? "url(#gradPinkPurple)" : "var(--lavender)";
  const dashArray = glow ? `${dash} ${c}` : `4 6`;
  const opacity = glow ? 1 : (pct > 0 ? 1 : 0.35);
  return `<svg width="120" height="120" viewBox="0 0 120 120">
    <defs><linearGradient id="gradPinkPurple" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#8060C0"/><stop offset="100%" stop-color="#C830F0"/>
    </linearGradient></defs>
    <circle cx="60" cy="60" r="${r}" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="10"/>
    <circle cx="60" cy="60" r="${r}" fill="none" stroke="${strokeColor}" stroke-width="10"
      stroke-linecap="round" stroke-dasharray="${glow ? dash + ' ' + c : dashArray}"
      transform="rotate(-90 60 60)" opacity="${opacity}"
      style="${glow ? 'filter: drop-shadow(0 0 6px var(--pink-glow));' : ''}"/>
  </svg>`;
}

async function loadPipeline() {
  const res = await fetch("/api/pipeline");
  const d = await res.json();

  const rings = [
    {label: "Seen", value: d.seen, max: d.seen, desc: "Total roles surfaced", glow: true},
    {label: "Emailed", value: d.emailed, max: d.seen, desc: "Cold emails sent", glow: false},
    {label: "Replied", value: d.replied, max: d.seen, desc: "Got a response", glow: false},
    {label: "Interview", value: d.interview, max: d.seen, desc: "Interview booked", glow: false},
  ];

  document.getElementById("pipeline-rings").innerHTML = rings.map(r => {
    const pct = r.max > 0 ? r.value / r.max : 0;
    return `<div class="ring-card">
      <div class="ring-svg-wrap">
        ${ringSvg(pct, r.glow)}
        <div class="ring-number mono">${r.value}</div>
      </div>
      <div class="ring-label">${r.label}</div>
      <div class="ring-desc">${r.desc}</div>
    </div>`;
  }).join("");

  const goalPct = Math.min(100, Math.round(100 * d.weekly_goal.current / d.weekly_goal.target));
  document.getElementById("pipeline-goal").innerHTML = `
    <div class="goal-top">
      <span class="goal-label">Weekly goal &mdash; emails sent</span>
      <span class="goal-counter mono">${d.weekly_goal.current}/${d.weekly_goal.target}</span>
    </div>
    <div class="goal-track"><div class="goal-fill" style="width:${goalPct}%"></div></div>`;

  document.getElementById("perf-tbody").innerHTML = d.sources.map(s => `
    <tr>
      <td class="name">${escapeHtml(s.name)}</td>
      <td class="num">${s.seen}</td>
      <td class="num">${s.sent}</td>
      <td class="num">${s.replies}</td>
    </tr>`).join("");
}

renderFilterRow();
loadAgent();
</script>

</body>
</html>
"""


if __name__ == "__main__":
    database.init_db()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
