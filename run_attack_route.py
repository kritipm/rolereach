"""
Attack Route runner — runs after the Priority Engine.

Determines attack routes and action plans for every ELIGIBLE/REVIEW job
that has a priority level assigned but has not yet been processed.
REJECT jobs are never processed.
"""

import database
import attack_route


def run():
    jobs = database.fetch_jobs_needing_attack_route()
    if not jobs:
        print("[attack_route] No jobs to process — nothing to do.")
        return

    print(f"[attack_route] Processing {len(jobs)} job(s)...")

    priorities = {"P1": 0, "P2": 0, "P3": 0}
    access_levels = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "NONE": 0}

    with database.get_connection() as conn:
        for job in jobs:
            result = attack_route.determine_attack_route(job)
            database.update_attack_route(conn, job["comment_id"], result)
            priorities[result["attack_priority"]] += 1
            access_levels[result["attack_access_level"]] += 1

        conn.commit()

    total = len(jobs)
    print(
        f"[attack_route] Done. "
        f"P1={priorities['P1']} "
        f"P2={priorities['P2']} "
        f"P3={priorities['P3']} "
        f"| Access HIGH={access_levels['HIGH']} "
        f"MEDIUM={access_levels['MEDIUM']} "
        f"LOW={access_levels['LOW']} "
        f"NONE={access_levels['NONE']} "
        f"/ {total} total"
    )


if __name__ == "__main__":
    run()
