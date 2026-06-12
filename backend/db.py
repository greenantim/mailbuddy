"""Local SQLite cache for Gmail message metadata.

We never store email bodies — only the lightweight headers we need to build
the sender dashboard (from, subject, date, labels). This makes the dashboard
instant even with tens of thousands of emails, and keeps everything on the
user's machine.
"""

import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "MAILBUDDY_DB",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "mailbuddy.db"),
)

# SQLite + threads: use a single shared connection guarded by a lock. The sync
# job and the request handlers can both touch the DB.
_local = threading.local()
_write_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def get_conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


@contextmanager
def write():
    """Serialize writes so the background sync and API don't clash."""
    with _write_lock:
        conn = get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_db():
    with write() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id            TEXT PRIMARY KEY,
                thread_id     TEXT,
                from_email    TEXT,
                from_name     TEXT,
                subject       TEXT,
                date_ts       INTEGER,
                label_ids     TEXT,
                list_unsub    TEXT,
                unsub_post    INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_from_email ON messages(from_email);
            CREATE INDEX IF NOT EXISTS idx_subject ON messages(subject);

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            -- Senders the user has flagged as VIP (by email address).
            CREATE TABLE IF NOT EXISTS vips (
                email TEXT PRIMARY KEY,
                name  TEXT
            );

            -- Senders the user has dismissed from the triage list ("handled").
            CREATE TABLE IF NOT EXISTS hidden_senders (
                email     TEXT PRIMARY KEY,
                name      TEXT,
                hidden_at INTEGER
            );
            """
        )


def set_meta(key, value):
    with write() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def get_meta(key, default=None):
    row = get_conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def upsert_messages(rows):
    """rows: list of dicts with message metadata."""
    with write() as conn:
        conn.executemany(
            """
            INSERT INTO messages
                (id, thread_id, from_email, from_name, subject, date_ts,
                 label_ids, list_unsub)
            VALUES
                (:id, :thread_id, :from_email, :from_name, :subject, :date_ts,
                 :label_ids, :list_unsub)
            ON CONFLICT(id) DO UPDATE SET
                label_ids=excluded.label_ids
            """,
            rows,
        )


def delete_messages(ids):
    if not ids:
        return
    with write() as conn:
        conn.executemany("DELETE FROM messages WHERE id=?", [(i,) for i in ids])


def remove_label_locally(ids, label_id):
    """Reflect a label removal (e.g. INBOX) in the cache without a re-sync."""
    if not ids:
        return
    with write() as conn:
        for mid in ids:
            row = conn.execute(
                "SELECT label_ids FROM messages WHERE id=?", (mid,)
            ).fetchone()
            if not row:
                continue
            labels = [l for l in (row["label_ids"] or "").split(",") if l]
            if label_id in labels:
                labels.remove(label_id)
                conn.execute(
                    "UPDATE messages SET label_ids=? WHERE id=?",
                    (",".join(labels), mid),
                )


def add_label_locally(ids, label_id):
    if not ids:
        return
    with write() as conn:
        for mid in ids:
            row = conn.execute(
                "SELECT label_ids FROM messages WHERE id=?", (mid,)
            ).fetchone()
            if not row:
                continue
            labels = [l for l in (row["label_ids"] or "").split(",") if l]
            if label_id not in labels:
                labels.append(label_id)
                conn.execute(
                    "UPDATE messages SET label_ids=? WHERE id=?",
                    (",".join(labels), mid),
                )


def sender_summary(search=None, sort="count", limit=500, after_ts=None,
                   before_ts=None, include_hidden=False):
    """Aggregate messages by sender for the dashboard.

    after_ts/before_ts (unix seconds) restrict to messages with date_ts within
    that range (inclusive), so the dashboard can be scoped to e.g. "older than
    a year" before a bulk cleanup.

    Senders the user has dismissed ("hidden") are excluded unless
    include_hidden is True, in which case each row carries an is_hidden flag.
    """
    sql = """
        SELECT
            from_email,
            MAX(from_name)                  AS from_name,
            COUNT(*)                        AS count,
            MAX(date_ts)                    AS last_ts,
            MAX(list_unsub IS NOT NULL AND list_unsub != '') AS has_unsub
        FROM messages
        WHERE from_email IS NOT NULL AND from_email != ''
    """
    params = []
    if search:
        sql += " AND (from_email LIKE ? OR from_name LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    if after_ts is not None:
        sql += " AND date_ts >= ?"
        params.append(after_ts)
    if before_ts is not None:
        sql += " AND date_ts <= ?"
        params.append(before_ts)
    if not include_hidden:
        sql += " AND from_email NOT IN (SELECT email FROM hidden_senders)"
    sql += " GROUP BY from_email"
    order = {
        "count": "count DESC",
        "recent": "last_ts DESC",
        "name": "from_name COLLATE NOCASE ASC",
    }.get(sort, "count DESC")
    sql += f" ORDER BY {order} LIMIT ?"
    params.append(limit)

    vips = {r["email"] for r in get_conn().execute("SELECT email FROM vips")}
    hidden = {r["email"] for r in get_conn().execute("SELECT email FROM hidden_senders")}
    out = []
    for r in get_conn().execute(sql, params):
        out.append(
            {
                "from_email": r["from_email"],
                "from_name": r["from_name"] or r["from_email"],
                "count": r["count"],
                "last_ts": r["last_ts"],
                "has_unsub": bool(r["has_unsub"]),
                "is_vip": r["from_email"] in vips,
                "is_hidden": r["from_email"] in hidden,
            }
        )
    return out


# --- Hidden / dismissed senders ---

def hide_sender(email, name=None):
    import time as _t
    with write() as conn:
        conn.execute(
            "INSERT INTO hidden_senders(email, name, hidden_at) VALUES(?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET name=excluded.name",
            (email, name, int(_t.time())),
        )


def unhide_sender(email):
    with write() as conn:
        conn.execute("DELETE FROM hidden_senders WHERE email=?", (email,))


def hidden_count():
    return get_conn().execute(
        "SELECT COUNT(*) c FROM hidden_senders"
    ).fetchone()["c"]


def message_ids_for_sender(from_email, only_inbox=False, after_ts=None, before_ts=None):
    sql = "SELECT id, label_ids FROM messages WHERE from_email=?"
    params = [from_email]
    if after_ts is not None:
        sql += " AND date_ts >= ?"
        params.append(after_ts)
    if before_ts is not None:
        sql += " AND date_ts <= ?"
        params.append(before_ts)
    rows = get_conn().execute(sql, params).fetchall()
    ids = []
    for r in rows:
        if only_inbox and "INBOX" not in (r["label_ids"] or "").split(","):
            continue
        ids.append(r["id"])
    return ids


def message_ids_for_subject(subject_like):
    rows = get_conn().execute(
        "SELECT id FROM messages WHERE subject LIKE ?", (f"%{subject_like}%",)
    ).fetchall()
    return [r["id"] for r in rows]


def unsub_for_sender(from_email):
    row = get_conn().execute(
        "SELECT list_unsub FROM messages "
        "WHERE from_email=? AND list_unsub IS NOT NULL AND list_unsub != '' "
        "ORDER BY date_ts DESC LIMIT 1",
        (from_email,),
    ).fetchone()
    return row["list_unsub"] if row else None


def total_count():
    return get_conn().execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]


def max_date_ts():
    row = get_conn().execute("SELECT MAX(date_ts) m FROM messages").fetchone()
    return row["m"] or 0


# --- VIPs ---

def add_vip(email, name=None):
    with write() as conn:
        conn.execute(
            "INSERT INTO vips(email, name) VALUES(?, ?) "
            "ON CONFLICT(email) DO UPDATE SET name=excluded.name",
            (email, name),
        )


def remove_vip(email):
    with write() as conn:
        conn.execute("DELETE FROM vips WHERE email=?", (email,))


def list_vips():
    return [dict(r) for r in get_conn().execute("SELECT * FROM vips ORDER BY name")]
