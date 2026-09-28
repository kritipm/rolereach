import sqlite3
import urllib.parse
from contextlib import contextmanager

import config

if config.DATABASE_URL:
    import psycopg2
    import psycopg2.extras

# Supabase's direct-connection host is IPv6-only and can fail with "Network is
# unreachable" on IPv4-only networks/runners; the pooler host supports both.
# The pooler also requires the username in "postgres.<project-ref>" form (plain
# "postgres" — valid on the direct host — is rejected by the pooler).
SUPABASE_POOLER_HOST = "aws-0-ap-northeast-1.pooler.supabase.com"
SUPABASE_POOLER_PORT = 6543
SUPABASE_POOLER_USERNAME = "postgres.zkrznztjdofityugbuou"


class Connection:
    """Wraps a sqlite3 or psycopg2 connection behind one interface so callers
    can do `conn.execute(query_with_question_marks, params).fetchall()` and get
    dict-like rows back regardless of backend."""

    def __init__(self, raw, is_postgres):
        self._raw = raw
        self.is_postgres = is_postgres
        if not is_postgres:
            raw.row_factory = sqlite3.Row

    def execute(self, query, params=()):
        if self.is_postgres:
            cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(query.replace("?", "%s"), params)
        else:
            cur = self._raw.cursor()
            cur.execute(query, params)
        return cur

    def commit(self):
        self._raw.commit()

    def close(self):
        self._raw.close()


def _connect_postgres(database_url):
    try:
        return psycopg2.connect(database_url)
    except psycopg2.OperationalError as original_error:
        message = str(original_error)
        if "Network is unreachable" not in message and "could not connect" not in message:
            raise

        # .username/.password are already percent-encoded substrings straight from
        # the URL (urlparse does not decode them) — reuse them as-is rather than
        # re-quoting, which would double-encode any password containing a "%".
        parsed = urllib.parse.urlparse(database_url)
        username = SUPABASE_POOLER_USERNAME if parsed.username == "postgres" else (parsed.username or "")
        fallback_netloc = f"{username}:{parsed.password or ''}@{SUPABASE_POOLER_HOST}:{SUPABASE_POOLER_PORT}"
        fallback_url = parsed._replace(netloc=fallback_netloc).geturl()

        try:
            return psycopg2.connect(fallback_url)
        except psycopg2.OperationalError:
            raise original_error


@contextmanager
def get_connection():
    if config.DATABASE_URL:
        raw = _connect_postgres(config.DATABASE_URL)
        conn = Connection(raw, is_postgres=True)
    else:
        raw = sqlite3.connect(config.DB_PATH)
        conn = Connection(raw, is_postgres=False)
    try:
        yield conn
    except Exception:
        if conn.is_postgres:
            conn._raw.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_connection() as conn:
        if conn.is_postgres:
            _init_postgres(conn)
        else:
            _init_sqlite(conn)
        _init_pipeline_events(conn)
        conn.commit()


def _init_pipeline_events(conn):
    if conn.is_postgres:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pipeline_events (
                id         BIGSERIAL PRIMARY KEY,
                job_id     BIGINT NOT NULL,
                event_type TEXT NOT NULL,
                noted_at   TEXT NOT NULL,
                UNIQUE (job_id, event_type)
            )
            """
        )
    else:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pipeline_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id     INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                noted_at   TEXT NOT NULL,
                UNIQUE (job_id, event_type)
            )
            """
        )


def _init_postgres(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            comment_id                  BIGINT PRIMARY KEY,
            thread_id                   BIGINT NOT NULL,
            author                      TEXT,
            posted_at                   TEXT,
            matched_keyword             TEXT,
            text                        TEXT NOT NULL,
            url                         TEXT,
            company_url                 TEXT,
            verified                    INTEGER NOT NULL DEFAULT 0,
            source                      TEXT NOT NULL DEFAULT 'hackernews',
            external_id                 TEXT,
            hm_name                     TEXT,
            hm_email                    TEXT,
            smtp_guesses                TEXT,
            company_linkedin            TEXT,
            email_draft                 TEXT,
            notified                    INTEGER NOT NULL DEFAULT 0,
            experience_range            TEXT,
            description                 TEXT,
            eligibility_status          TEXT,
            eligibility_reason          TEXT,
            role_category               TEXT,
            experience_status           TEXT,
            location_status             TEXT,
            employment_status           TEXT,
            seniority_status            TEXT,
            freshness_status            TEXT,
            education_status            TEXT,
            work_authorization_status   TEXT,
            role_fit_score              REAL,
            role_fit_level              TEXT,
            experience_fit_score        REAL,
            experience_fit_level        TEXT,
            skill_fit_score             REAL,
            skill_fit_level             TEXT,
            portfolio_fit_score         REAL,
            portfolio_fit_level         TEXT,
            domain_fit_score            REAL,
            domain_fit_level            TEXT,
            overall_fit_score           REAL,
            overall_fit_level           TEXT,
            fit_evidence                TEXT,
            priority_score              REAL,
            priority_level              TEXT,
            priority_fit_score_used     REAL,
            priority_freshness_score    REAL,
            priority_access_points      INTEGER,
            priority_access_score       REAL,
            product_person_linkedin     TEXT,
            product_person_name         TEXT,
            product_person_role         TEXT,
            product_folks_linkedin      TEXT,
            attack_access_level         TEXT,
            attack_priority             TEXT,
            attack_intensity            TEXT,
            attack_available_routes     TEXT,
            attack_primary_route        TEXT,
            attack_first_action         TEXT,
            attack_action_sequence      TEXT,
            attack_primary_contact      TEXT,
            attack_primary_contact_linkedin TEXT,
            attack_attributed_email     TEXT,
            attack_job_application_url  TEXT,
            attack_reason               TEXT,
            linkedin_draft              TEXT,
            execution_packet            TEXT
        )
        """
    )
    # Add eligibility + fit + priority + attack + execution columns to pre-existing tables (idempotent)
    for col, typedef in [
        ("eligibility_status", "TEXT"),
        ("eligibility_reason", "TEXT"),
        ("role_category", "TEXT"),
        ("experience_status", "TEXT"),
        ("location_status", "TEXT"),
        ("employment_status", "TEXT"),
        ("seniority_status", "TEXT"),
        ("freshness_status", "TEXT"),
        ("education_status", "TEXT"),
        ("work_authorization_status", "TEXT"),
        ("role_fit_score", "REAL"),
        ("role_fit_level", "TEXT"),
        ("experience_fit_score", "REAL"),
        ("experience_fit_level", "TEXT"),
        ("skill_fit_score", "REAL"),
        ("skill_fit_level", "TEXT"),
        ("portfolio_fit_score", "REAL"),
        ("portfolio_fit_level", "TEXT"),
        ("domain_fit_score", "REAL"),
        ("domain_fit_level", "TEXT"),
        ("overall_fit_score", "REAL"),
        ("overall_fit_level", "TEXT"),
        ("fit_evidence", "TEXT"),
        ("priority_score", "REAL"),
        ("priority_level", "TEXT"),
        ("priority_fit_score_used", "REAL"),
        ("priority_freshness_score", "REAL"),
        ("priority_access_points", "INTEGER"),
        ("priority_access_score", "REAL"),
        ("product_person_linkedin", "TEXT"),
        ("product_person_name", "TEXT"),
        ("product_person_role", "TEXT"),
        ("product_folks_linkedin", "TEXT"),
        ("attack_access_level", "TEXT"),
        ("attack_priority", "TEXT"),
        ("attack_intensity", "TEXT"),
        ("attack_available_routes", "TEXT"),
        ("attack_primary_route", "TEXT"),
        ("attack_first_action", "TEXT"),
        ("attack_action_sequence", "TEXT"),
        ("attack_primary_contact", "TEXT"),
        ("attack_primary_contact_linkedin", "TEXT"),
        ("attack_attributed_email", "TEXT"),
        ("attack_job_application_url", "TEXT"),
        ("attack_reason", "TEXT"),
        ("linkedin_draft", "TEXT"),
        ("execution_packet", "TEXT"),
    ]:
        conn.execute(f"ALTER TABLE jobs ADD COLUMN IF NOT EXISTS {col} {typedef}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_actions (
            job_id      BIGINT PRIMARY KEY,
            status      TEXT NOT NULL,
            actioned_at TEXT NOT NULL
        )
        """
    )


def _init_sqlite(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            comment_id                  INTEGER PRIMARY KEY,
            thread_id                   INTEGER NOT NULL,
            author                      TEXT,
            posted_at                   TEXT,
            matched_keyword             TEXT,
            text                        TEXT NOT NULL,
            url                         TEXT,
            company_url                 TEXT,
            verified                    INTEGER NOT NULL DEFAULT 0,
            source                      TEXT NOT NULL DEFAULT 'hackernews',
            external_id                 TEXT,
            hm_name                     TEXT,
            hm_email                    TEXT,
            smtp_guesses                TEXT,
            company_linkedin            TEXT,
            email_draft                 TEXT,
            notified                    INTEGER NOT NULL DEFAULT 0,
            experience_range            TEXT,
            description                 TEXT,
            eligibility_status          TEXT,
            eligibility_reason          TEXT,
            role_category               TEXT,
            experience_status           TEXT,
            location_status             TEXT,
            employment_status           TEXT,
            seniority_status            TEXT,
            freshness_status            TEXT,
            education_status            TEXT,
            work_authorization_status   TEXT,
            role_fit_score              REAL,
            role_fit_level              TEXT,
            experience_fit_score        REAL,
            experience_fit_level        TEXT,
            skill_fit_score             REAL,
            skill_fit_level             TEXT,
            portfolio_fit_score         REAL,
            portfolio_fit_level         TEXT,
            domain_fit_score            REAL,
            domain_fit_level            TEXT,
            overall_fit_score           REAL,
            overall_fit_level           TEXT,
            fit_evidence                TEXT,
            priority_score              REAL,
            priority_level              TEXT,
            priority_fit_score_used     REAL,
            priority_freshness_score    REAL,
            priority_access_points      INTEGER,
            priority_access_score       REAL,
            product_person_linkedin     TEXT,
            product_person_name         TEXT,
            product_person_role         TEXT,
            product_folks_linkedin      TEXT,
            attack_access_level         TEXT,
            attack_priority             TEXT,
            attack_intensity            TEXT,
            attack_available_routes     TEXT,
            attack_primary_route        TEXT,
            attack_first_action         TEXT,
            attack_action_sequence      TEXT,
            attack_primary_contact      TEXT,
            attack_primary_contact_linkedin TEXT,
            attack_attributed_email     TEXT,
            attack_job_application_url  TEXT,
            attack_reason               TEXT,
            linkedin_draft              TEXT,
            execution_packet            TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_actions (
            job_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            actioned_at TEXT NOT NULL
        )
        """
    )
    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "company_url" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN company_url TEXT")
    if "verified" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
    if "source" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'hackernews'")
    if "external_id" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN external_id TEXT")
    if "hm_name" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN hm_name TEXT")
    if "hm_email" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN hm_email TEXT")
    if "smtp_guesses" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN smtp_guesses TEXT")
    if "company_linkedin" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN company_linkedin TEXT")
    if "email_draft" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN email_draft TEXT")
    if "notified" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN notified INTEGER NOT NULL DEFAULT 0")
    if "experience_range" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN experience_range TEXT")
    if "description" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN description TEXT")
    if "eligibility_status" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN eligibility_status TEXT")
    if "eligibility_reason" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN eligibility_reason TEXT")
    if "role_category" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN role_category TEXT")
    if "experience_status" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN experience_status TEXT")
    if "location_status" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN location_status TEXT")
    if "employment_status" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN employment_status TEXT")
    if "seniority_status" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN seniority_status TEXT")
    if "freshness_status" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN freshness_status TEXT")
    if "education_status" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN education_status TEXT")
    if "work_authorization_status" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN work_authorization_status TEXT")
    if "role_fit_score" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN role_fit_score REAL")
    if "role_fit_level" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN role_fit_level TEXT")
    if "experience_fit_score" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN experience_fit_score REAL")
    if "experience_fit_level" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN experience_fit_level TEXT")
    if "skill_fit_score" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN skill_fit_score REAL")
    if "skill_fit_level" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN skill_fit_level TEXT")
    if "portfolio_fit_score" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN portfolio_fit_score REAL")
    if "portfolio_fit_level" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN portfolio_fit_level TEXT")
    if "domain_fit_score" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN domain_fit_score REAL")
    if "domain_fit_level" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN domain_fit_level TEXT")
    if "overall_fit_score" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN overall_fit_score REAL")
    if "overall_fit_level" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN overall_fit_level TEXT")
    if "fit_evidence" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN fit_evidence TEXT")
    if "priority_score" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN priority_score REAL")
    if "priority_level" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN priority_level TEXT")
    if "priority_fit_score_used" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN priority_fit_score_used REAL")
    if "priority_freshness_score" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN priority_freshness_score REAL")
    if "priority_access_points" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN priority_access_points INTEGER")
    if "priority_access_score" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN priority_access_score REAL")
    if "product_person_linkedin" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN product_person_linkedin TEXT")
    if "product_person_name" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN product_person_name TEXT")
    if "product_person_role" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN product_person_role TEXT")
    if "product_folks_linkedin" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN product_folks_linkedin TEXT")
    if "attack_access_level" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN attack_access_level TEXT")
    if "attack_priority" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN attack_priority TEXT")
    if "attack_intensity" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN attack_intensity TEXT")
    if "attack_available_routes" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN attack_available_routes TEXT")
    if "attack_primary_route" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN attack_primary_route TEXT")
    if "attack_first_action" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN attack_first_action TEXT")
    if "attack_action_sequence" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN attack_action_sequence TEXT")
    if "attack_primary_contact" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN attack_primary_contact TEXT")
    if "attack_primary_contact_linkedin" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN attack_primary_contact_linkedin TEXT")
    if "attack_attributed_email" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN attack_attributed_email TEXT")
    if "attack_job_application_url" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN attack_job_application_url TEXT")
    if "attack_reason" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN attack_reason TEXT")
    if "linkedin_draft" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN linkedin_draft TEXT")
    if "execution_packet" not in existing_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN execution_packet TEXT")


def save_job(conn, job):
    values = (
        job["comment_id"],
        job["thread_id"],
        job["author"],
        job["posted_at"],
        job["matched_keyword"],
        job["text"],
        job["url"],
        job["company_url"],
        int(job["verified"]),
        job.get("source", "hackernews"),
        job.get("external_id"),
        job.get("experience_range", "Not specified"),
        job.get("description"),
    )
    columns = """(comment_id, thread_id, author, posted_at, matched_keyword, text, url,
             company_url, verified, source, external_id, experience_range, description)"""

    if conn.is_postgres:
        # Extract title from text field — format is "Title | Location | via Source"
        text = job.get("text", "")
        title = text.split("|")[0].strip().lower() if text else ""
        author = (job.get("author") or "").strip().lower()

        if title and author:
            existing = conn.execute(
                """
                SELECT comment_id FROM jobs
                WHERE LOWER(TRIM(SPLIT_PART(text, '|', 1))) = ?
                AND LOWER(TRIM(author)) = ?
                LIMIT 1
                """,
                (title, author)
            ).fetchone()
            if existing:
                return

    if conn.is_postgres:
        conn.execute(
            f"""
            INSERT INTO jobs {columns}
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (comment_id) DO NOTHING
            """,
            values,
        )
    else:
        conn.execute(
            f"""
            INSERT OR IGNORE INTO jobs {columns}
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )


def fetch_all_jobs():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY posted_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def fetch_unchecked_jobs():
    """Return all jobs that have not yet been assessed by the eligibility gate."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE eligibility_status IS NULL"
        ).fetchall()
        return [dict(row) for row in rows]


def update_eligibility(conn, comment_id, result):
    """Write eligibility assessment fields back to a job row."""
    conn.execute(
        """
        UPDATE jobs SET
            eligibility_status        = ?,
            eligibility_reason        = ?,
            role_category             = ?,
            experience_status         = ?,
            location_status           = ?,
            employment_status         = ?,
            seniority_status          = ?,
            freshness_status          = ?,
            education_status          = ?,
            work_authorization_status = ?
        WHERE comment_id = ?
        """,
        (
            result["eligibility_status"],
            result["eligibility_reason"],
            result["role_category"],
            result["experience_status"],
            result["location_status"],
            result["employment_status"],
            result["seniority_status"],
            result["freshness_status"],
            result["education_status"],
            result["work_authorization_status"],
            comment_id,
        ),
    )


def fetch_jobs_needing_fit_assessment():
    """Return ELIGIBLE/REVIEW jobs that have not yet been scored by fit assessment."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE eligibility_status IN ('ELIGIBLE', 'REVIEW')
              AND role_fit_score IS NULL
            """
        ).fetchall()
        return [dict(row) for row in rows]


def update_fit_assessment(conn, comment_id, result):
    """Write all fit assessment scores and evidence back to a job row."""
    conn.execute(
        """
        UPDATE jobs SET
            role_fit_score          = ?,
            role_fit_level          = ?,
            experience_fit_score    = ?,
            experience_fit_level    = ?,
            skill_fit_score         = ?,
            skill_fit_level         = ?,
            portfolio_fit_score     = ?,
            portfolio_fit_level     = ?,
            domain_fit_score        = ?,
            domain_fit_level        = ?,
            overall_fit_score       = ?,
            overall_fit_level       = ?,
            fit_evidence            = ?
        WHERE comment_id = ?
        """,
        (
            result["role_fit_score"],
            result["role_fit_level"],
            result["experience_fit_score"],
            result["experience_fit_level"],
            result["skill_fit_score"],
            result["skill_fit_level"],
            result["portfolio_fit_score"],
            result["portfolio_fit_level"],
            result["domain_fit_score"],
            result["domain_fit_level"],
            result["overall_fit_score"],
            result["overall_fit_level"],
            result["fit_evidence"],
            comment_id,
        ),
    )


def fetch_jobs_needing_enrichment(limit=None):
    with get_connection() as conn:
        query = """
            SELECT * FROM jobs
            WHERE company_url IS NOT NULL AND TRIM(company_url) != ''
              AND (hm_email IS NULL OR TRIM(hm_email) = '')
              AND (eligibility_status IS NULL OR eligibility_status != 'REJECT')
        """
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        return [dict(row) for row in conn.execute(query).fetchall()]


def update_hiring_manager(comment_id, hm_name, hm_email):
    with get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET hm_name = ?, hm_email = ? WHERE comment_id = ?",
            (hm_name, hm_email, comment_id),
        )
        conn.commit()


def update_smtp_guesses(comment_id, smtp_guesses):
    with get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET smtp_guesses = ? WHERE comment_id = ?",
            (smtp_guesses, comment_id),
        )
        conn.commit()


def update_company_linkedin(comment_id, company_linkedin):
    with get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET company_linkedin = ? WHERE comment_id = ?",
            (company_linkedin, comment_id),
        )
        conn.commit()


def fetch_jobs_with_company_url():
    with get_connection() as conn:
        query = """
            SELECT * FROM jobs
            WHERE company_url IS NOT NULL AND TRIM(company_url) != ''
        """
        return [dict(row) for row in conn.execute(query).fetchall()]


def fetch_jobs_needing_draft():
    with get_connection() as conn:
        query = """
            SELECT * FROM jobs
            WHERE hm_email IS NOT NULL AND TRIM(hm_email) != ''
              AND (email_draft IS NULL OR TRIM(email_draft) = '')
              AND (eligibility_status IS NULL OR eligibility_status != 'REJECT')
        """
        return [dict(row) for row in conn.execute(query).fetchall()]


def update_email_draft(comment_id, email_draft):
    with get_connection() as conn:
        # Reset execution_packet so Layer 5 rebuilds it with the new email content
        conn.execute(
            "UPDATE jobs SET email_draft = ?, execution_packet = NULL WHERE comment_id = ?",
            (email_draft, comment_id),
        )
        conn.commit()


def fetch_unnotified_jobs(source=None):
    with get_connection() as conn:
        if source:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE notified = 0 AND source = ?
                  AND (eligibility_status IS NULL OR eligibility_status != 'REJECT')
                """,
                (source,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE notified = 0
                  AND (eligibility_status IS NULL OR eligibility_status != 'REJECT')
                """
            ).fetchall()
        return [dict(row) for row in rows]


def mark_notified(comment_id):
    with get_connection() as conn:
        conn.execute("UPDATE jobs SET notified = 1 WHERE comment_id = ?", (comment_id,))
        conn.commit()


def set_job_action(job_id, status, actioned_at):
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_actions (job_id, status, actioned_at)
            VALUES (?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET status = excluded.status, actioned_at = excluded.actioned_at
            """,
            (job_id, status, actioned_at),
        )
        conn.commit()


def fetch_all_job_actions():
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM user_actions").fetchall()
        return {row["job_id"]: dict(row) for row in rows}


def fetch_jobs_needing_attack_route():
    """Return ELIGIBLE/REVIEW jobs that have a priority level but no attack route yet."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE eligibility_status IN ('ELIGIBLE', 'REVIEW')
              AND priority_level IS NOT NULL
              AND attack_access_level IS NULL
            """
        ).fetchall()
        return [dict(row) for row in rows]


def update_attack_route(conn, comment_id, result):
    """Write attack route assessment back to a job row."""
    conn.execute(
        """
        UPDATE jobs SET
            attack_access_level             = ?,
            attack_priority                 = ?,
            attack_intensity                = ?,
            attack_available_routes         = ?,
            attack_primary_route            = ?,
            attack_first_action             = ?,
            attack_action_sequence          = ?,
            attack_primary_contact          = ?,
            attack_primary_contact_linkedin = ?,
            attack_attributed_email         = ?,
            attack_job_application_url      = ?,
            attack_reason                   = ?
        WHERE comment_id = ?
        """,
        (
            result["attack_access_level"],
            result["attack_priority"],
            result["attack_intensity"],
            result["attack_available_routes"],
            result["attack_primary_route"],
            result["attack_first_action"],
            result["attack_action_sequence"],
            result["attack_primary_contact"],
            result["attack_primary_contact_linkedin"],
            result["attack_attributed_email"],
            result["attack_job_application_url"],
            result["attack_reason"],
            comment_id,
        ),
    )


def fetch_jobs_needing_execution_packet():
    """Return jobs that have an attack plan but no execution packet yet."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE attack_access_level IS NOT NULL
              AND execution_packet IS NULL
              AND (eligibility_status IS NULL OR eligibility_status != 'REJECT')
            """
        ).fetchall()
        return [dict(row) for row in rows]


def update_execution_packet(conn, comment_id, result):
    """Write execution packet and LinkedIn draft back to a job row."""
    conn.execute(
        """
        UPDATE jobs SET
            linkedin_draft   = ?,
            execution_packet = ?
        WHERE comment_id = ?
        """,
        (
            result["linkedin_draft"],
            result["execution_packet"],
            comment_id,
        ),
    )


def fetch_jobs_needing_priority():
    """Return ELIGIBLE/REVIEW jobs with completed fit assessment but no priority score."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE eligibility_status IN ('ELIGIBLE', 'REVIEW')
              AND role_fit_score IS NOT NULL
              AND priority_score IS NULL
            """
        ).fetchall()
        return [dict(row) for row in rows]


def update_priority(conn, comment_id, result):
    """Write priority score and component inputs back to a job row."""
    conn.execute(
        """
        UPDATE jobs SET
            priority_score           = ?,
            priority_level           = ?,
            priority_fit_score_used  = ?,
            priority_freshness_score = ?,
            priority_access_points   = ?,
            priority_access_score    = ?
        WHERE comment_id = ?
        """,
        (
            result["priority_score"],
            result["priority_level"],
            result["priority_fit_score_used"],
            result["priority_freshness_score"],
            result["priority_access_points"],
            result["priority_access_score"],
            comment_id,
        ),
    )


# ---------- Pipeline events (Layer 6) ----------

def upsert_pipeline_event(job_id, event_type, noted_at):
    """Insert or replace a pipeline event for a job."""
    with get_connection() as conn:
        if conn.is_postgres:
            conn.execute(
                """
                INSERT INTO pipeline_events (job_id, event_type, noted_at)
                VALUES (?, ?, ?)
                ON CONFLICT (job_id, event_type) DO UPDATE SET noted_at = EXCLUDED.noted_at
                """,
                (job_id, event_type, noted_at),
            )
        else:
            conn.execute(
                """
                INSERT OR REPLACE INTO pipeline_events (job_id, event_type, noted_at)
                VALUES (?, ?, ?)
                """,
                (job_id, event_type, noted_at),
            )
        conn.commit()


def delete_pipeline_event(job_id, event_type):
    """Remove a pipeline event for a job."""
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM pipeline_events WHERE job_id = ? AND event_type = ?",
            (job_id, event_type),
        )
        conn.commit()


def fetch_pipeline_events(job_id=None):
    """
    Fetch pipeline events.

    With job_id: returns list of event dicts for that job.
    Without job_id: returns dict {job_id: [event dicts]}.
    """
    with get_connection() as conn:
        if job_id is not None:
            rows = conn.execute(
                "SELECT * FROM pipeline_events WHERE job_id = ? ORDER BY noted_at",
                (job_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        else:
            rows = conn.execute(
                "SELECT * FROM pipeline_events ORDER BY job_id, noted_at"
            ).fetchall()
            by_job = {}
            for row in rows:
                d = dict(row)
                jid = d["job_id"]
                if jid not in by_job:
                    by_job[jid] = []
                by_job[jid].append(d)
            return by_job
