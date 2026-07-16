import argparse
import subprocess
import sys
import time
from datetime import datetime

import schedule

PIPELINE_STEPS = [
    "main.py",              # Hacker News
    "scraper_cutshort.py",
    "scraper_iimjobs.py",
    "scraper_google_jobs.py",
    "scraper_internshala.py",
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


def run_pipeline():
    started_at = datetime.now()
    print(f"\n### RoleReach pipeline run started at {started_at.isoformat(timespec='seconds')} ###")

    for script_name in PIPELINE_STEPS:
        run_step(script_name)

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
