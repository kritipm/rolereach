"""
Fit Assessment runner — runs after the eligibility gate.

Evaluates every ELIGIBLE or REVIEW job that has not yet been scored
and writes the fit scores back to the DB.
REJECT jobs are skipped — they never reach downstream steps.
"""

import database
import fit_assessment


def run():
    jobs = database.fetch_jobs_needing_fit_assessment()
    if not jobs:
        print("[fit_assessment] No jobs to score — nothing to do.")
        return

    print(f"[fit_assessment] Scoring {len(jobs)} job(s)...")

    levels = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}

    with database.get_connection() as conn:
        for job in jobs:
            result = fit_assessment.assess_fit(job)
            database.update_fit_assessment(conn, job["comment_id"], result)
            levels[result["role_fit_level"]] += 1

        conn.commit()

    total = len(jobs)
    print(
        f"[fit_assessment] Done. "
        f"Role Fit — HIGH={levels['HIGH']} "
        f"MEDIUM={levels['MEDIUM']} "
        f"LOW={levels['LOW']} "
        f"/ {total} total"
    )


if __name__ == "__main__":
    run()
