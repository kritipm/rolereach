"""
Eligibility gate — runs after all scrapers, before enrichment.

Evaluates every job that has not yet been assessed (eligibility_status IS NULL)
and writes the result back to the DB.  Jobs marked REJECT are excluded from
enrichment, drafting, and Telegram notifications by the downstream DB queries.
"""

import argparse

import database
import eligibility


def run(rerun_all=False):
    if rerun_all:
        jobs = database.fetch_all_jobs_for_eligibility()
        label = "all"
    else:
        jobs = database.fetch_unchecked_jobs()
        label = "unchecked"

    if not jobs:
        print(f"[eligibility] No {label} jobs — nothing to do.")
        return

    print(f"[eligibility] Evaluating {len(jobs)} {label} job(s)...")
    counts = {"ELIGIBLE": 0, "REVIEW": 0, "REJECT": 0}

    with database.get_connection() as conn:
        for job in jobs:
            result = eligibility.check_eligibility(job)
            database.update_eligibility(conn, job["comment_id"], result)
            counts[result["eligibility_status"]] += 1

        conn.commit()

    total = len(jobs)
    print(
        f"[eligibility] Done. "
        f"ELIGIBLE={counts['ELIGIBLE']} "
        f"REVIEW={counts['REVIEW']} "
        f"REJECT={counts['REJECT']} "
        f"/ {total} total"
    )
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--rerun-all",
        action="store_true",
        help="Re-evaluate ALL jobs (including already-rejected ones)",
    )
    args = parser.parse_args()
    run(rerun_all=args.rerun_all)
