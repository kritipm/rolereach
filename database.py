import sqlite3
import urllib.parse
from contextlib import contextmanager

import config

if config.DATABASE_URL:
    import psycopg2
    import psycopg2.extras

# Supabase's direct-connection host is IPv6-only and can fail with "Network is
# unreachable" on IPv4-only networks/runners; the pooler host supports both.
SUPABASE_POOLER_HOST = "aws-0-ap-northeast-1.pooler.supabase.com"
SUPABASE_POOLER_PORT = 6543


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
        fallback_netloc = f"{parsed.username or ''}:{parsed.password or ''}@{SUPABASE_POOLER_HOST}:{SUPABASE_POOLER_PORT}"
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
    finally:
        conn.close()


def init_db():
    with get_connection() as conn:
        if conn.is_postgres:
            _init_postgres(conn)
        else:
            _init_sqlite(conn)
        conn.commit()


def _init_postgres(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            comment_id       BIGINT PRIMARY KEY,
            thread_id        BIGINT NOT NULL,
            author           TEXT,
            posted_at        TEXT,
            matched_keyword  TEXT,
            text             TEXT NOT NULL,
            url              TEXT,
            company_url      TEXT,
            verified         INTEGER NOT NULL DEFAULT 0,
            source           TEXT NOT NULL DEFAULT 'hackernews',
            external_id      TEXT,
            hm_name          TEXT,
            hm_email         TEXT,
            smtp_guesses     TEXT,
            company_linkedin TEXT,
            email_draft      TEXT,
            notified         INTEGER NOT NULL DEFAULT 0,
            experience_range TEXT,
            description      TEXT
        )
        """
    )
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
            comment_id INTEGER PRIMARY KEY,
            thread_id INTEGER NOT NULL,
            author TEXT,
            posted_at TEXT,
            matched_keyword TEXT,
            text TEXT NOT NULL,
            url TEXT,
            company_url TEXT,
            verified INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'hackernews',
            external_id TEXT,
            hm_name TEXT,
            hm_email TEXT,
            smtp_guesses TEXT,
            company_linkedin TEXT,
            email_draft TEXT,
            notified INTEGER NOT NULL DEFAULT 0,
            experience_range TEXT,
            description TEXT
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


def fetch_jobs_needing_enrichment(limit=None):
    with get_connection() as conn:
        query = """
            SELECT * FROM jobs
            WHERE company_url IS NOT NULL AND TRIM(company_url) != ''
              AND (hm_email IS NULL OR TRIM(hm_email) = '')
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
        """
        return [dict(row) for row in conn.execute(query).fetchall()]


def update_email_draft(comment_id, email_draft):
    with get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET email_draft = ? WHERE comment_id = ?",
            (email_draft, comment_id),
        )
        conn.commit()


def fetch_unnotified_jobs(source=None):
    with get_connection() as conn:
        if source:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE notified = 0 AND source = ?", (source,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM jobs WHERE notified = 0").fetchall()
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
