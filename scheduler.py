import argparse
import subprocess
import sys
import time
from datetime import datetime

import requests
import schedule

import config

PIPELINE_STEPS = [
    "main.py",
    "scraper_cutshort.py",
    "scraper_iimjobs.py",
    "scraper_google_jobs.py",
    "scraper_jsearch.py",
    "scraper_internshala.py",
    "scraper_careers.py",
    "scraper_yc.py",
    "enricher_snov.py",
    "drafter_claude.py",
    "telegram_bot.py",
]


def run_step(script_name):
    print(f"\n{'=' * 60}")
    print(f"STEP: {script_name}")
    print(f"{'=' * 60}")

    result = subprocess.run([sys.executable, script_name])

    if result.returncode != 0:
        print(f"[{script_name}] exited with code {result.returncode} — continuing to next step.")


def sync_db_to_railway():
    print(f"\n{'=' * 60}")
    print("STEP: sync rolereach.db to Railway dashboard")
    print(f"{'=' * 60}")

    if config.DATABASE_URL:
        print("DATABASE_URL is set — pipeline writes directly to Postgres, skipping legacy sqlite sync.")
        return

    if not config.RAILWAY_TOKEN:
        print("RAILWAY_TOKEN not set in .env — skipping sync.")
        return

    url = f"{config.DASHBOARD_URL}/api/sync-db"
    try:
        with open(config.DB_PATH, "rb") as db_file:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {config.RAILWAY_TOKEN}"},
                files={"db": ("rolereach.db", db_file, "application/octet-stream")},
                timeout=60,
            )
        if response.ok:
            print(f"Synced rolereach.db to {config.DASHBOARD_URL} — {response.json()}")
        else:
            print(f"Sync failed ({response.status_code}): {response.text} — continuing.")
    except requests.RequestException as exc:
        print(f"Sync request to {url} failed: {exc} — continuing.")


def run_pipeline():
    started_at = datetime.now()
    print(f"\n### RoleReach pipeline run started at {started_at.isoformat(timespec='seconds')} ###")

    for script_name in PIPELINE_STEPS:
        run_step(script_name)

    sync_db_to_railway()

    finished_at = datetime.now()
    print(f"\n### RoleReach pipeline run finished at {finished_at.isoformat(timespec='seconds')} "
          f"(took {finished_at - started_at}) ###")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", action="store_true", help="Run the pipeline immediately, once, then exit")
    args = parser.parse_args()

    if args.now:
        run_pipeline()
    else:
        schedule.every().day.at("08:00").do(run_pipeline)
        print("Scheduler started — pipeline will run daily at 08:00 IST (local system time).")
        while True:
            schedule.run_pending()
            time.sleep(60)
