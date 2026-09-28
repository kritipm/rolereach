"""
Priority Engine runner — runs after fit assessment.

Scores every ELIGIBLE/REVIEW job that has completed fit assessment but
has not yet received a priority score.  REJECT jobs are never scored.
"""

import database
import priority_engine


def run():
    jobs = database.fetch_jobs_needing_priority()
    if not jobs:
        print("[priority] No jobs to score — nothing to do.")
        return

    print(f"[priority] Scoring {len(jobs)} job(s)...")

    levels = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "PENDING": 0}

    with database.get_connection() as conn:
        for job in jobs:
            result = priority_engine.calculate_priority(job)
            database.update_priority(conn, job["comment_id"], result)
            levels[result["priority_level"]] += 1

        conn.commit()

    total = len(jobs)
    print(
        f"[priority] Done. "
        f"HIGH={levels['HIGH']} "
        f"MEDIUM={levels['MEDIUM']} "
        f"LOW={levels['LOW']} "
        f"PENDING={levels['PENDING']} "
        f"/ {total} total"
    )


if __name__ == "__main__":
    run()
