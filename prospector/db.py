"""SQLite state. The DB is the source of truth for what has been contacted.

Every stage reads and writes status here, so re-running any command is safe:
nothing gets discovered twice, audited twice, or emailed twice.

Lead status flow:
    NEW -> AUDITED -> DRAFTED -> SENT -> REPLIED
    NEW -> SKIPPED   (did not qualify, or on the suppression list)
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

STATUSES = ("NEW", "AUDITED", "DRAFTED", "SENT", "REPLIED", "SKIPPED", "FAILED")

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY,
    place_id        TEXT UNIQUE,
    name            TEXT NOT NULL,
    category        TEXT,
    address         TEXT,
    city            TEXT,
    phone           TEXT,
    website         TEXT,
    rating          REAL,
    review_count    INTEGER,
    maps_url        TEXT,
    source          TEXT,
    search_niche    TEXT,
    search_location TEXT,
    status          TEXT NOT NULL DEFAULT 'NEW',
    qualified       INTEGER,
    lead_type       TEXT,          -- NO_SITE | BAD_SITE
    skip_reason     TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audits (
    lead_id         INTEGER PRIMARY KEY REFERENCES leads(id) ON DELETE CASCADE,
    reachable       INTEGER,
    blocked         INTEGER,
    final_url       TEXT,
    https           INTEGER,
    status_code     INTEGER,
    load_ms         INTEGER,
    mobile_friendly INTEGER,
    copyright_year  INTEGER,
    page_title      TEXT,
    issues_json     TEXT,
    score           INTEGER,
    headline_issue  TEXT,
    audited_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY,
    lead_id     INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,     -- email | phone
    value       TEXT NOT NULL,
    found_on    TEXT,
    UNIQUE(lead_id, kind, value)
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    lead_id     INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    channel     TEXT NOT NULL,     -- email | whatsapp
    to_value    TEXT,
    subject     TEXT,
    body        TEXT NOT NULL,
    wa_link     TEXT,
    status      TEXT NOT NULL DEFAULT 'DRAFT',  -- DRAFT | SENT | FAILED
    provider_id TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    sent_at     TEXT,
    UNIQUE(lead_id, channel)
);

-- Never contact anything in here. Checked before every draft and every send.
CREATE TABLE IF NOT EXISTS suppression (
    value       TEXT PRIMARY KEY,  -- lowercased email, domain, or phone
    kind        TEXT NOT NULL,     -- email | domain | phone
    reason      TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets a reader (the web UI) coexist with a writing batch, and the busy
    # timeout makes a second writer wait instead of dying with 'database is
    # locked' and losing its work.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations, so an existing database keeps working."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(audits)")}
    for column, ddl in (("blocked", "INTEGER"),):
        if column not in have:
            conn.execute(f"ALTER TABLE audits ADD COLUMN {column} {ddl}")


@contextmanager
def session(path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# leads
# --------------------------------------------------------------------------


def upsert_lead(conn: sqlite3.Connection, lead: dict[str, Any]) -> tuple[int, bool]:
    """Insert a lead. Returns (lead_id, created). Existing leads are left alone
    so a re-run of `find` never resets someone you already contacted."""
    place_id = lead.get("place_id")
    row = None
    if place_id:
        row = conn.execute(
            "SELECT id FROM leads WHERE place_id = ?", (place_id,)
        ).fetchone()
    if row is None:
        # The API and the scraper mint different place ids for the same shop, so
        # fall back to name + city — but ONLY when one side lacks a place_id.
        # Two rows both carrying distinct Google place_ids are two different
        # businesses that happen to share a name; merging them silently dropped
        # one and attached its contact email to the other.
        candidate = conn.execute(
            "SELECT id, place_id FROM leads WHERE lower(name) = lower(?) AND lower(COALESCE(city,'')) = lower(?)",
            (lead["name"], lead.get("city") or ""),
        ).fetchone()
        if candidate is not None and not (
            place_id and candidate["place_id"] and candidate["place_id"] != place_id
        ):
            row = candidate
    if row:
        return row["id"], False

    ts = now()
    cur = conn.execute(
        """
        INSERT INTO leads (place_id, name, category, address, city, phone, website,
                           rating, review_count, maps_url, source, search_niche,
                           search_location, status, created_at, updated_at)
        VALUES (:place_id, :name, :category, :address, :city, :phone, :website,
                :rating, :review_count, :maps_url, :source, :search_niche,
                :search_location, 'NEW', :created_at, :updated_at)
        """,
        {
            "place_id": place_id,
            "name": lead["name"],
            "category": lead.get("category"),
            "address": lead.get("address"),
            "city": lead.get("city"),
            "phone": lead.get("phone"),
            "website": lead.get("website"),
            "rating": lead.get("rating"),
            "review_count": lead.get("review_count"),
            "maps_url": lead.get("maps_url"),
            "source": lead.get("source"),
            "search_niche": lead.get("search_niche"),
            "search_location": lead.get("search_location"),
            "created_at": ts,
            "updated_at": ts,
        },
    )
    return int(cur.lastrowid), True


def set_status(
    conn: sqlite3.Connection,
    lead_id: int,
    status: str,
    *,
    skip_reason: str | None = None,
    qualified: bool | None = None,
    lead_type: str | None = None,
) -> None:
    fields = ["status = ?", "updated_at = ?"]
    values: list[Any] = [status, now()]
    if skip_reason is not None:
        fields.append("skip_reason = ?")
        values.append(skip_reason)
    if qualified is not None:
        fields.append("qualified = ?")
        values.append(1 if qualified else 0)
    if lead_type is not None:
        fields.append("lead_type = ?")
        values.append(lead_type)
    values.append(lead_id)
    conn.execute(f"UPDATE leads SET {', '.join(fields)} WHERE id = ?", values)


def leads_by_status(
    conn: sqlite3.Connection, status: str, limit: int | None = None
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM leads WHERE status = ? ORDER BY id"
    params: list[Any] = [status]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def get_lead(conn: sqlite3.Connection, lead_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()


# --------------------------------------------------------------------------
# audits
# --------------------------------------------------------------------------


def save_audit(conn: sqlite3.Connection, lead_id: int, audit: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO audits (lead_id, reachable, blocked, final_url, https, status_code,
                            load_ms, mobile_friendly, copyright_year, page_title,
                            issues_json, score, headline_issue, audited_at)
        VALUES (:lead_id, :reachable, :blocked, :final_url, :https, :status_code,
                :load_ms, :mobile_friendly, :copyright_year, :page_title,
                :issues_json, :score, :headline_issue, :audited_at)
        ON CONFLICT(lead_id) DO UPDATE SET
            reachable=excluded.reachable, blocked=excluded.blocked,
            final_url=excluded.final_url,
            https=excluded.https, status_code=excluded.status_code,
            load_ms=excluded.load_ms, mobile_friendly=excluded.mobile_friendly,
            copyright_year=excluded.copyright_year, page_title=excluded.page_title,
            issues_json=excluded.issues_json, score=excluded.score,
            headline_issue=excluded.headline_issue, audited_at=excluded.audited_at
        """,
        {"lead_id": lead_id, "audited_at": now(), **audit},
    )


def get_audit(conn: sqlite3.Connection, lead_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM audits WHERE lead_id = ?", (lead_id,)).fetchone()


# --------------------------------------------------------------------------
# contacts
# --------------------------------------------------------------------------


def add_contact(
    conn: sqlite3.Connection, lead_id: int, kind: str, value: str, found_on: str | None
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO contacts (lead_id, kind, value, found_on) VALUES (?,?,?,?)",
        (lead_id, kind, value.strip().lower() if kind == "email" else value, found_on),
    )


def best_email(conn: sqlite3.Connection, lead_id: int) -> str | None:
    rows = conn.execute(
        "SELECT value FROM contacts WHERE lead_id = ? AND kind = 'email'", (lead_id,)
    ).fetchall()
    if not rows:
        return None
    # Prefer a human-looking mailbox over a role account nobody reads.
    ranked = sorted(
        (r["value"] for r in rows),
        key=lambda e: (
            e.split("@")[0] in {"noreply", "no-reply", "donotreply"},
            e.split("@")[0] in {"support", "sales", "webmaster", "admin"},
            len(e),
        ),
    )
    return ranked[0]


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------


def save_message(
    conn: sqlite3.Connection,
    lead_id: int,
    channel: str,
    body: str,
    *,
    subject: str | None = None,
    to_value: str | None = None,
    wa_link: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO messages (lead_id, channel, to_value, subject, body, wa_link,
                              status, created_at)
        VALUES (?,?,?,?,?,?, 'DRAFT', ?)
        ON CONFLICT(lead_id, channel) DO UPDATE SET
            to_value=excluded.to_value, subject=excluded.subject,
            body=excluded.body, wa_link=excluded.wa_link
        """,
        (lead_id, channel, to_value, subject, body, wa_link, now()),
    )
    if cur.lastrowid:
        return int(cur.lastrowid)
    row = conn.execute(
        "SELECT id FROM messages WHERE lead_id = ? AND channel = ?", (lead_id, channel)
    ).fetchone()
    return int(row["id"])


def pending_messages(
    conn: sqlite3.Connection, channel: str, limit: int | None = None
) -> list[sqlite3.Row]:
    sql = """
        SELECT m.*, l.name AS business_name, l.phone AS business_phone
        FROM messages m JOIN leads l ON l.id = m.lead_id
        WHERE m.channel = ? AND m.status = 'DRAFT' AND l.status NOT IN ('REPLIED','SKIPPED')
        ORDER BY m.id
    """
    params: list[Any] = [channel]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def mark_message_sent(
    conn: sqlite3.Connection, message_id: int, provider_id: str | None
) -> None:
    # COALESCE keeps the FIRST send time: replaying a stale mark-sent command
    # must not rewrite history about when someone was actually contacted.
    conn.execute(
        "UPDATE messages SET status='SENT', sent_at=COALESCE(sent_at, ?), "
        "provider_id=COALESCE(provider_id, ?) WHERE id=?",
        (now(), provider_id, message_id),
    )


def mark_message_failed(conn: sqlite3.Connection, message_id: int, error: str) -> None:
    conn.execute(
        "UPDATE messages SET status='FAILED', error=? WHERE id=?", (error, message_id)
    )


# --------------------------------------------------------------------------
# suppression
# --------------------------------------------------------------------------


def suppress(conn: sqlite3.Connection, value: str, kind: str, reason: str) -> None:
    value = value.strip().lower()
    # Phones are stored digits-only because that is exactly how is_suppressed
    # queries them. Storing "+971 50 ..." as typed made the entry unmatchable:
    # the user was told the number was suppressed while it kept being queued.
    if kind == "phone":
        value = "".join(c for c in value if c.isdigit())
    if not value:
        raise ValueError("nothing to suppress after normalisation")
    conn.execute(
        "INSERT OR REPLACE INTO suppression (value, kind, reason, created_at) VALUES (?,?,?,?)",
        (value, kind, reason, now()),
    )


def is_suppressed(conn: sqlite3.Connection, email: str | None, phone: str | None) -> str | None:
    """Return the matching suppression reason, or None."""
    if email:
        email = email.strip().lower()
        row = conn.execute(
            "SELECT reason FROM suppression WHERE value = ? AND kind = 'email'", (email,)
        ).fetchone()
        if row:
            return row["reason"] or "email suppressed"
        domain = email.split("@")[-1]
        row = conn.execute(
            "SELECT reason FROM suppression WHERE value = ? AND kind = 'domain'", (domain,)
        ).fetchone()
        if row:
            return row["reason"] or "domain suppressed"
    if phone:
        digits = "".join(c for c in phone if c.isdigit())
        row = conn.execute(
            "SELECT reason FROM suppression WHERE value = ? AND kind = 'phone'", (digits,)
        ).fetchone()
        if row:
            return row["reason"] or "phone suppressed"
    return None


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM leads GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}
