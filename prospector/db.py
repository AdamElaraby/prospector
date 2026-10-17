"""SQLite state. The DB is the source of truth for what has been contacted.

Every stage reads and writes status here, so re-running any command is safe:
nothing gets discovered twice, audited twice, or emailed twice.

Lead status flow (what the machine has done):
    NEW -> AUDITED -> DRAFTED -> SENT -> REPLIED
    NEW -> SKIPPED   (did not qualify, or on the suppression list)

Sales pipeline (what the money has done) is tracked separately in `pipeline`:
    REVIEWED -> CONTACTED -> PROPOSAL_REQUESTED -> QUOTED -> WON
    (LOST from anywhere)

The two are deliberately not the same field. "SENT" says a message left the
machine; "CONTACTED" says a human conversation started. Counting leads is easy
and means nothing — the pipeline is what lets you count revenue.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

STATUSES = ("NEW", "AUDITED", "DRAFTED", "SENT", "REPLIED", "SKIPPED", "FAILED")

# Ordered. A stage implies every stage before it, so setting QUOTED backfills
# the earlier timestamps rather than pretending the deal skipped them.
PIPELINE_STAGES = (
    "REVIEWED",
    "CONTACTED",
    "PROPOSAL_REQUESTED",
    "QUOTED",
    "WON",
)
# Not on the ladder: a deal can be lost from any stage, and nothing follows it.
LOST = "LOST"
ALL_STAGES = PIPELINE_STAGES + (LOST,)

STAGE_TIMESTAMP = {
    "REVIEWED": "reviewed_at",
    "CONTACTED": "contacted_at",
    "PROPOSAL_REQUESTED": "proposal_requested_at",
    "QUOTED": "quoted_at",
    "WON": "won_at",
    "LOST": "lost_at",
}

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
    lead_score      INTEGER,       -- 0-100, see qualify.py
    lead_grade      TEXT,          -- A | B | C | D
    score_json      TEXT,          -- component breakdown + reasons
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
    -- Commercial opportunity. Deliberately separate columns from the technical
    -- defect fields above: "no way to book online" is a missing sales capability,
    -- not a bug, and conflating the two makes the pitch sound like a complaint.
    has_booking     INTEGER,
    has_quote_form  INTEGER,
    has_services    INTEGER,
    has_contact     INTEGER,
    ecommerce       INTEGER,
    gaps_json       TEXT,
    opportunity     INTEGER,       -- 0-100, higher = more commercial upside
    headline_gap    TEXT,
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

-- Where the money actually is. One row per lead, created on first stage change.
CREATE TABLE IF NOT EXISTS pipeline (
    lead_id               INTEGER PRIMARY KEY REFERENCES leads(id) ON DELETE CASCADE,
    stage                 TEXT NOT NULL,
    reviewed_at           TEXT,
    contacted_at          TEXT,
    proposal_requested_at TEXT,
    quoted_at             TEXT,
    won_at                TEXT,
    lost_at               TEXT,
    quote_amount          REAL,
    won_amount            REAL,
    currency              TEXT,
    lost_reason           TEXT,
    notes                 TEXT,
    updated_at            TEXT NOT NULL
);

-- Append-only history, so a stage set by mistake and corrected still leaves a
-- trace and a week's conversion numbers can be recomputed honestly.
CREATE TABLE IF NOT EXISTS pipeline_events (
    id          INTEGER PRIMARY KEY,
    lead_id     INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,
    amount      REAL,
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_pipeline_stage ON pipeline(stage);
CREATE INDEX IF NOT EXISTS idx_pipeline_events_lead ON pipeline_events(lead_id);
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
    additions = {
        "audits": (
            ("blocked", "INTEGER"),
            ("has_booking", "INTEGER"),
            ("has_quote_form", "INTEGER"),
            ("has_services", "INTEGER"),
            ("has_contact", "INTEGER"),
            ("ecommerce", "INTEGER"),
            ("gaps_json", "TEXT"),
            ("opportunity", "INTEGER"),
            ("headline_gap", "TEXT"),
        ),
        "leads": (
            ("lead_score", "INTEGER"),
            ("lead_grade", "TEXT"),
            ("score_json", "TEXT"),
        ),
    }
    for table, columns in additions.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for column, ddl in columns:
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


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
    lead_score: int | None = None,
    lead_grade: str | None = None,
    score_json: str | None = None,
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
    if lead_score is not None:
        fields.append("lead_score = ?")
        values.append(int(lead_score))
    if lead_grade is not None:
        fields.append("lead_grade = ?")
        values.append(lead_grade)
    if score_json is not None:
        fields.append("score_json = ?")
        values.append(score_json)
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


# Filled in only when a page was actually fetched and parsed.
AUDIT_OPTIONAL = (
    "has_booking", "has_quote_form", "has_services", "has_contact",
    "ecommerce", "gaps_json", "opportunity", "headline_gap",
)


def save_audit(conn: sqlite3.Connection, lead_id: int, audit: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO audits (lead_id, reachable, blocked, final_url, https, status_code,
                            load_ms, mobile_friendly, copyright_year, page_title,
                            issues_json, score, headline_issue,
                            has_booking, has_quote_form, has_services, has_contact,
                            ecommerce, gaps_json, opportunity, headline_gap, audited_at)
        VALUES (:lead_id, :reachable, :blocked, :final_url, :https, :status_code,
                :load_ms, :mobile_friendly, :copyright_year, :page_title,
                :issues_json, :score, :headline_issue,
                :has_booking, :has_quote_form, :has_services, :has_contact,
                :ecommerce, :gaps_json, :opportunity, :headline_gap, :audited_at)
        ON CONFLICT(lead_id) DO UPDATE SET
            reachable=excluded.reachable, blocked=excluded.blocked,
            final_url=excluded.final_url,
            https=excluded.https, status_code=excluded.status_code,
            load_ms=excluded.load_ms, mobile_friendly=excluded.mobile_friendly,
            copyright_year=excluded.copyright_year, page_title=excluded.page_title,
            issues_json=excluded.issues_json, score=excluded.score,
            headline_issue=excluded.headline_issue,
            has_booking=excluded.has_booking, has_quote_form=excluded.has_quote_form,
            has_services=excluded.has_services, has_contact=excluded.has_contact,
            ecommerce=excluded.ecommerce, gaps_json=excluded.gaps_json,
            opportunity=excluded.opportunity, headline_gap=excluded.headline_gap,
            audited_at=excluded.audited_at
        """,
        {
            "lead_id": lead_id,
            "audited_at": now(),
            # Named placeholders fail hard on a missing key, and the commercial
            # columns are absent whenever there was no page to look at.
            **{k: None for k in AUDIT_OPTIONAL},
            **audit,
        },
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


# --------------------------------------------------------------------------
# sales pipeline
# --------------------------------------------------------------------------


def _stage_rank(stage: str) -> int:
    """Position on the ladder. LOST sits off it, so it never 'outranks' WON."""
    try:
        return PIPELINE_STAGES.index(stage)
    except ValueError:
        return -1


def get_pipeline(conn: sqlite3.Connection, lead_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM pipeline WHERE lead_id = ?", (lead_id,)
    ).fetchone()


def set_stage(
    conn: sqlite3.Connection,
    lead_id: int,
    stage: str,
    *,
    amount: float | None = None,
    note: str | None = None,
    reason: str | None = None,
    currency: str | None = None,
    advance_only: bool = False,
) -> str | None:
    """Move a lead to `stage`. Returns the stage now recorded, or None if the
    call was a no-op under `advance_only`.

    Reaching a stage means every earlier stage happened, so their timestamps
    are backfilled: a deal you only remember to record once it is WON must not
    leave the funnel claiming nobody was ever contacted.

    `advance_only` is for the automatic callers (sending a message, marking a
    reply). They may push a lead forward but must never drag a deal you have
    already quoted or won back down the ladder.
    """
    if stage not in ALL_STAGES:
        raise ValueError(f"unknown stage {stage!r}; use one of {', '.join(ALL_STAGES)}")

    ts = now()
    row = get_pipeline(conn, lead_id)
    current = row["stage"] if row else None

    if advance_only and current:
        # Nothing automatic may undo a human's record of a real conversation.
        if current == LOST:
            return None
        if stage != LOST and _stage_rank(stage) <= _stage_rank(current):
            return None

    if row is None:
        conn.execute(
            "INSERT INTO pipeline (lead_id, stage, currency, updated_at) VALUES (?,?,?,?)",
            (lead_id, stage, currency, ts),
        )
        row = get_pipeline(conn, lead_id)

    fields = ["stage = ?", "updated_at = ?"]
    values: list[Any] = [stage, ts]

    # Backfill: this stage's timestamp, plus any earlier one still empty. LOST
    # backfills nothing — losing a deal says nothing about how far it got.
    to_stamp = [stage]
    if stage != LOST:
        to_stamp = [s for s in PIPELINE_STAGES[: _stage_rank(stage) + 1]]
    for earlier in to_stamp:
        column = STAGE_TIMESTAMP[earlier]
        if row[column] is None:
            fields.append(f"{column} = ?")
            values.append(ts)

    # A deliberate move backwards is a correction: the later stages did not
    # actually happen. Clearing their timestamps is what stops `funnel`, which
    # counts who ever reached a stage, from going on reporting a win you have
    # just taken back. Only manual callers get here — advance_only returned
    # above rather than moving anything down.
    if current and stage != LOST and _stage_rank(stage) < _stage_rank(current):
        for later in PIPELINE_STAGES[_stage_rank(stage) + 1:]:
            column = STAGE_TIMESTAMP[later]
            if row[column] is not None:
                fields.append(f"{column} = NULL")
            if later == "WON" and amount is None:
                fields.append("won_amount = NULL")

    if amount is not None:
        # One number, two meanings: what you asked for, and what you got.
        column = "won_amount" if stage == "WON" else "quote_amount"
        fields.append(f"{column} = ?")
        values.append(float(amount))
        if stage == "WON" and row["quote_amount"] is None:
            fields.append("quote_amount = ?")
            values.append(float(amount))
    if currency:
        fields.append("currency = ?")
        values.append(currency)
    if reason is not None:
        fields.append("lost_reason = ?")
        values.append(reason)
    if note is not None:
        fields.append("notes = ?")
        values.append(note)

    values.append(lead_id)
    conn.execute(f"UPDATE pipeline SET {', '.join(fields)} WHERE lead_id = ?", values)
    conn.execute(
        "INSERT INTO pipeline_events (lead_id, stage, amount, note, created_at) VALUES (?,?,?,?,?)",
        (lead_id, stage, amount, note or reason, ts),
    )
    return stage


def funnel(conn: sqlite3.Connection) -> dict[str, int]:
    """How many leads have EVER reached each stage.

    Counting by current stage would show an empty 'contacted' column the moment
    those conversations turned into quotes, which reads as though outreach
    stopped working. Timestamps are cumulative, so they answer the real
    question: of everyone contacted, how many asked for a proposal?
    """
    result: dict[str, int] = {}
    for stage in PIPELINE_STAGES:
        column = STAGE_TIMESTAMP[stage]
        result[stage] = conn.execute(
            f"SELECT COUNT(*) n FROM pipeline WHERE {column} IS NOT NULL"
        ).fetchone()["n"]
    result[LOST] = conn.execute(
        "SELECT COUNT(*) n FROM pipeline WHERE stage = 'LOST'"
    ).fetchone()["n"]
    return result


def revenue(conn: sqlite3.Connection) -> dict[str, Any]:
    """Money won, money still open, and the rates between the two."""
    won = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(won_amount), 0) total FROM pipeline WHERE stage = 'WON'"
    ).fetchone()
    # Open = quoted and still live. A quote on a lost deal is not a pipeline.
    open_row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(quote_amount), 0) total FROM pipeline "
        "WHERE quoted_at IS NOT NULL AND stage NOT IN ('WON', 'LOST')"
    ).fetchone()
    currency_row = conn.execute(
        "SELECT currency, COUNT(*) n FROM pipeline WHERE currency IS NOT NULL "
        "GROUP BY currency ORDER BY n DESC LIMIT 1"
    ).fetchone()
    steps = funnel(conn)
    contacted = steps.get("CONTACTED", 0)
    won_n = int(won["n"])
    return {
        "won_count": won_n,
        "won_total": float(won["total"] or 0),
        "open_count": int(open_row["n"]),
        "open_total": float(open_row["total"] or 0),
        "avg_deal": (float(won["total"] or 0) / won_n) if won_n else 0.0,
        "currency": currency_row["currency"] if currency_row else None,
        "close_rate": (won_n / contacted * 100) if contacted else 0.0,
        "funnel": steps,
    }


def pipeline_rows(
    conn: sqlite3.Connection, stage: str | None = None, limit: int = 300
) -> list[sqlite3.Row]:
    sql = """
        SELECT p.*, l.name, l.city, l.category, l.phone, l.website,
               l.lead_score, l.lead_grade, l.status
        FROM pipeline p JOIN leads l ON l.id = p.lead_id
    """
    params: list[Any] = []
    if stage and stage != "ALL":
        sql += " WHERE p.stage = ?"
        params.append(stage)
    sql += " ORDER BY p.updated_at DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


# --------------------------------------------------------------------------
# timestamps: when each thing happened to a lead
# --------------------------------------------------------------------------

# Timestamps are stored as UTC ISO-8601 throughout. Grouping and display work
# in LOCAL time, because "which day did I find these?" is a question about the
# user's day, not about UTC — a lead found at 1am in Dubai belongs to that
# morning's batch, not to the previous one.


def local_day(iso: str | None) -> str | None:
    """UTC ISO-8601 -> local YYYY-MM-DD."""
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return iso[:10]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%Y-%m-%d")


# SQLite's own localtime conversion, so grouping happens in the query rather
# than by pulling every row into Python to bucket it.
LOCAL_DAY_SQL = "date(l.created_at, 'localtime')"

# The category a lead is filed under. Google's own category when it gave one,
# otherwise the niche that was searched for — scrapers frequently return no
# category, and filing all of those under "uncategorised" would hide exactly
# the comparison worth making.
CATEGORY_SQL = (
    "COALESCE(NULLIF(TRIM(l.category), ''), NULLIF(TRIM(l.search_niche), ''), "
    "'uncategorised')"
)


def lead_timeline(conn: sqlite3.Connection, lead_id: int) -> list[dict[str, Any]]:
    """Everything that has happened to one lead, oldest first.

    Assembled from the timestamps each stage already writes rather than a
    separate event log, so it cannot drift out of step with the real state.
    """
    events: list[tuple[str | None, str]] = []

    lead = get_lead(conn, lead_id)
    if lead is None:
        return []
    events.append((lead["created_at"], "found"))

    audit = get_audit(conn, lead_id)
    if audit is not None:
        events.append((audit["audited_at"], "website audited"))

    for row in conn.execute(
        "SELECT channel, created_at, sent_at FROM messages WHERE lead_id = ?", (lead_id,)
    ):
        events.append((row["created_at"], f"{row['channel']} message drafted"))
        if row["sent_at"]:
            events.append((row["sent_at"], f"{row['channel']} message sent"))

    stage_row = get_pipeline(conn, lead_id)
    if stage_row is not None:
        for stage in ALL_STAGES:
            when = stage_row[STAGE_TIMESTAMP[stage]]
            if when:
                events.append((when, stage.replace("_", " ").lower()))

    # A missing timestamp sorts last rather than crashing the sort on None.
    ordered = sorted((e for e in events if e[0]), key=lambda e: e[0])
    return [{"at": at, "day": local_day(at), "what": what} for at, what in ordered]


def leads_by_day(
    conn: sqlite3.Connection,
    limit: int = 60,
    where: list[str] | None = None,
    params: list[Any] | None = None,
) -> list[sqlite3.Row]:
    """Discovery days, newest first, with how the batch turned out.

    This is the view that answers "was Tuesday's search worth running?" — the
    one number a lead count on its own never gives you.

    Takes the same filters as the flat listing. A summary that quietly ignored
    them would answer a different question from the one that was asked.
    """
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return conn.execute(
        f"""
        SELECT {LOCAL_DAY_SQL} AS day,
               COUNT(*) AS found,
               SUM(COALESCE(l.qualified, 0)) AS qualified,
               SUM(l.status IN ('SENT', 'REPLIED')) AS contacted,
               AVG(l.lead_score) AS avg_score
        FROM leads l{clause}
        GROUP BY day
        ORDER BY day DESC
        LIMIT ?
        """,
        [*(params or []), limit],
    ).fetchall()


def leads_by_category(
    conn: sqlite3.Connection,
    limit: int = 60,
    where: list[str] | None = None,
    params: list[Any] | None = None,
) -> list[sqlite3.Row]:
    """Categories, best average lead score first.

    Falls back to the niche that was searched for: scrapers often return no
    category at all, and bucketing all of those under "unknown" would hide the
    one comparison worth making — which trades are actually worth prospecting.
    """
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return conn.execute(
        f"""
        SELECT {CATEGORY_SQL} AS category,
               COUNT(*) AS found,
               SUM(COALESCE(l.qualified, 0)) AS qualified,
               SUM(l.status IN ('SENT', 'REPLIED')) AS contacted,
               AVG(l.lead_score) AS avg_score
        FROM leads l{clause}
        GROUP BY category
        ORDER BY (avg_score IS NULL), avg_score DESC, found DESC
        LIMIT ?
        """,
        [*(params or []), limit],
    ).fetchall()


def distinct_days(conn: sqlite3.Connection) -> list[str]:
    return [
        r["day"]
        for r in conn.execute(
            # Both SQL fragments are written against the `l` alias, so every
            # query using them has to supply it.
            f"SELECT DISTINCT {LOCAL_DAY_SQL} AS day FROM leads l ORDER BY day DESC"
        )
    ]


def distinct_categories(conn: sqlite3.Connection) -> list[str]:
    return [
        r["category"]
        for r in conn.execute(
            f"SELECT DISTINCT {CATEGORY_SQL} AS category FROM leads l ORDER BY category"
        )
    ]
