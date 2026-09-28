"""
Execution Packet runner — runs after the email drafter.

Builds the ready-to-execute packet for every job that has an attack plan
but no execution packet yet.  Runs cleanly if there's nothing to do.
"""

import database
import execution_packet


def run():
    jobs = database.fetch_jobs_needing_execution_packet()
    if not jobs:
        print("[execution_packet] No jobs to build — nothing to do.")
        return

    print(f"[execution_packet] Building packets for {len(jobs)} job(s)...")

    with database.get_connection() as conn:
        for job in jobs:
            result = execution_packet.build_execution_packet(job)
            database.update_execution_packet(conn, job["comment_id"], result)

        conn.commit()

    print(f"[execution_packet] Done. {len(jobs)} packets built.")


if __name__ == "__main__":
    run()
