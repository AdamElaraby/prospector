"""Local web UI. Runs the same CLI commands, just with buttons.

    .venv/bin/python -m prospector.web

Binds to 127.0.0.1 only and requires a token that is printed at startup, so a
random page in your browser cannot drive a server that holds your API keys and
can send mail. Cross-origin requests are rejected outright.
"""

from __future__ import annotations

# Must run before any dependency import: a Rosetta shell launches the x86_64
# slice of the universal Python, and every native wheel then fails to load.
from prospector._arch import ensure_native

ensure_native("prospector.web")

import json
import mimetypes
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import db, progress, qualify
from .audit import describe_issues, domain_of
from .opportunity import describe_gaps, gap_labels
from .config import ROOT, load_config
from .config_io import KNOWN_KEYS, read_env_keys, update_config, write_env_keys

HERE = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(24)

RUNNABLE = {"find", "audit", "draft", "send", "wa", "export"}

STAGE_LABEL = {
    "REVIEWED": "reviewed",
    "CONTACTED": "conversation started",
    "PROPOSAL_REQUESTED": "proposal requested",
    "QUOTED": "quote sent",
    "WON": "won",
    "LOST": "lost",
}


# --------------------------------------------------------------------- job runner


class Job:
    """One CLI subprocess at a time, with its output tailed for the UI."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.lines: deque[str] = deque(maxlen=4000)
        self.command: list[str] = []
        self.returncode: int | None = None
        self.running = False
        # The last progress marker the subprocess emitted. This is what drives
        # the bar, so it is real position, never a timer pretending to be one.
        self.progress: dict | None = None

    def start(self, args: list[str]) -> tuple[bool, str]:
        with self.lock:
            if self.running:
                return False, "Something is already running. Wait for it to finish."
            self.lines.clear()
            self.returncode = None
            self.running = True
            self.command = args
            self.progress = None

        cmd = [sys.executable, "-m", "prospector.cli", *args]
        # PROSPECTOR_PROGRESS turns on the machine-readable markers the bar
        # reads. Only set here, so a person at a terminal never sees them.
        env = {
            **os.environ,
            "COLUMNS": "160",
            "PYTHONUNBUFFERED": "1",
            "PROSPECTOR_PROGRESS": "1",
        }

        def pump() -> None:
            try:
                self.proc = subprocess.Popen(
                    cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                )
                assert self.proc.stdout is not None
                for line in self.proc.stdout:
                    line = line.rstrip("\n")
                    payload = progress.parse(line)
                    if payload is not None:
                        # A marker is state, not output: showing it in the log
                        # would bury the stage's real messages in JSON.
                        self.progress = payload
                        continue
                    self.lines.append(line)
                self.proc.wait()
                self.returncode = self.proc.returncode
            except Exception as exc:  # surface the failure in the UI log
                self.lines.append(f"[error] {exc}")
                self.returncode = -1
            finally:
                self.running = False
                if self.progress:
                    self.progress = {**self.progress, "finished": True}

        threading.Thread(target=pump, daemon=True).start()
        return True, "started"

    def stop(self) -> bool:
        proc = self.proc
        if self.running and proc and proc.poll() is None:
            proc.terminate()
            return True
        return False

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "command": self.command,
            "returncode": self.returncode,
            "lines": list(self.lines),
            "progress": self.progress,
        }


JOB = Job()


# ------------------------------------------------------------------- data helpers


def _cfg():
    return load_config()


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def state_payload() -> dict[str, Any]:
    cfg = _cfg()
    with db.session(cfg.db_path) as conn:
        counts = db.counts(conn)
        message_counts = _rows(
            conn, "SELECT channel, status, COUNT(*) n FROM messages GROUP BY channel, status"
        )
        suppressed = conn.execute("SELECT COUNT(*) n FROM suppression").fetchone()["n"]
        money = db.revenue(conn)
        grades = _rows(
            conn,
            "SELECT lead_grade, COUNT(*) n FROM leads WHERE qualified = 1 "
            "AND lead_grade IS NOT NULL GROUP BY lead_grade ORDER BY lead_grade",
        )

    sender = dict(cfg.get("sender", {}) or {})
    search = cfg.get("search", {}) or {}
    return {
        "sender": sender,
        "sender_missing": cfg.check_sender_ready(),
        "email_channel": dict(cfg.get("email_channel", {}) or {}),
        "email_enabled": cfg.email_enabled,
        "email_missing": cfg.check_email_ready(),
        "search": {
            "niches": list(search.get("niches") or []),
            "locations": list(search.get("locations") or []),
            "max_results_per_query": search.get("max_results_per_query"),
            "source": search.get("source"),
            "apify_actor_id": cfg.get("search.apify.actor_id"),
        },
        "qualify": {
            "bad_site_threshold": cfg.get("qualify.bad_site_threshold"),
            "min_review_count": cfg.get("qualify.min_review_count"),
            "include_no_site": cfg.get("qualify.include_no_site"),
            "include_bad_site": cfg.get("qualify.include_bad_site"),
            "min_lead_score": cfg.get("qualify.min_lead_score", 55),
            "min_activity_score": cfg.get("qualify.min_activity_score", 12),
            "min_problem_score": cfg.get("qualify.min_problem_score", 10),
        },
        "llm": {"provider": cfg.get("llm.provider"), "model": cfg.get("llm.model")},
        "keys": read_env_keys(),
        "counts": counts,
        "messages": message_counts,
        "suppressed": suppressed,
        "grades": {g["lead_grade"]: g["n"] for g in grades},
        "revenue": money,
        "stage_labels": STAGE_LABEL,
        "pipeline_currency": cfg.get("pipeline.currency") or money.get("currency") or "",
        "job": JOB.snapshot(),
    }


def leads_payload(
    status: str | None,
    limit: int,
    category: str | None = None,
    day: str | None = None,
    since: str | None = None,
) -> list[dict]:
    cfg = _cfg()
    sql = f"""
        SELECT l.id, l.name, l.category, l.city, l.phone, l.website, l.rating,
               l.created_at, l.updated_at,
               {db.LOCAL_DAY_SQL} AS day,
               {db.CATEGORY_SQL} AS group_category,
               a.audited_at,
               l.review_count, l.status, l.lead_type, l.skip_reason,
               l.lead_score, l.lead_grade,
               a.score, a.headline_issue, a.opportunity, a.gaps_json,
               a.has_booking, a.has_quote_form, a.has_services, a.has_contact,
               p.stage AS pipeline_stage, p.won_amount, p.quote_amount,
               (SELECT value FROM contacts c WHERE c.lead_id=l.id AND c.kind='email' LIMIT 1) AS email,
               (SELECT channel FROM messages m WHERE m.lead_id=l.id LIMIT 1) AS channel
        FROM leads l
        LEFT JOIN audits a ON a.lead_id = l.id
        LEFT JOIN pipeline p ON p.lead_id = l.id
    """
    where: list[str] = []
    params: list[Any] = []
    if status and status != "ALL":
        where.append("l.status = ?")
        params.append(status)
    if category and category != "ALL":
        where.append(f"{db.CATEGORY_SQL} = ?")
        params.append(category)
    if day and day != "ALL":
        where.append(f"{db.LOCAL_DAY_SQL} = ?")
        params.append(day)
    if since:
        where.append(f"{db.LOCAL_DAY_SQL} >= ?")
        params.append(since)
    if where:
        sql += " WHERE " + " AND ".join(where)
    # Newest batch first, then best lead within it, scored on qualification
    # rather than on how broken the site is: a dead business with a terrible
    # website is not a better lead than a busy one with a mediocre site. The
    # day comes first so the UI can draw a heading each time it changes.
    sql += (
        " ORDER BY day DESC,"
        " COALESCE(l.qualified, 0) DESC,"
        " COALESCE(l.lead_score, -1) DESC,"
        " (a.score IS NULL), a.score DESC, l.id LIMIT ?"
    )
    params.append(limit)
    with db.session(cfg.db_path) as conn:
        rows = _rows(conn, sql, tuple(params))
    for row in rows:
        row["domain"] = domain_of(row.get("website"))
        row["gaps"] = gap_labels(row.get("gaps_json"))
    return rows


def groups_payload(
    status: str | None = None, category: str | None = None, since: str | None = None
) -> dict:
    """Per-day and per-category summaries, plus the values to filter by."""
    cfg = _cfg()
    where: list[str] = []
    params: list[Any] = []
    if status and status != "ALL":
        where.append("l.status = ?")
        params.append(status)
    if category and category != "ALL":
        where.append(f"{db.CATEGORY_SQL} = ?")
        params.append(category)
    if since:
        where.append(f"{db.LOCAL_DAY_SQL} >= ?")
        params.append(since)
    with db.session(cfg.db_path) as conn:
        return {
            "by_day": [dict(r) for r in db.leads_by_day(conn, 90, where, params)],
            "by_category": [dict(r) for r in db.leads_by_category(conn, 90, where, params)],
            "days": db.distinct_days(conn),
            "categories": db.distinct_categories(conn),
        }


def lead_detail(lead_id: int) -> dict | None:
    cfg = _cfg()
    with db.session(cfg.db_path) as conn:
        lead = db.get_lead(conn, lead_id)
        if not lead:
            return None
        audit_row = db.get_audit(conn, lead_id)
        contacts = _rows(conn, "SELECT kind, value FROM contacts WHERE lead_id=?", (lead_id,))
        messages = _rows(conn, "SELECT * FROM messages WHERE lead_id=?", (lead_id,))
        stage_row = db.get_pipeline(conn, lead_id)
        timeline = db.lead_timeline(conn, lead_id)
    detail = dict(lead)
    detail["timeline"] = timeline
    detail["found_day"] = db.local_day(lead["created_at"])
    detail["audit"] = dict(audit_row) if audit_row else None
    detail["issues"] = describe_issues(audit_row["issues_json"]) if audit_row else []
    # Commercial gaps stay a separate list from technical issues all the way
    # to the screen, because they are a different kind of claim.
    detail["gaps"] = describe_gaps(audit_row["gaps_json"]) if audit_row else []
    detail["verdict"] = qualify.explain(lead["score_json"])
    detail["pipeline"] = dict(stage_row) if stage_row else None
    detail["contacts"] = contacts
    detail["messages"] = messages
    return detail


def messages_payload(channel: str | None, status: str | None) -> list[dict]:
    cfg = _cfg()
    sql = """
        SELECT m.*, l.name AS business_name, l.lead_type, l.city
        FROM messages m JOIN leads l ON l.id = m.lead_id WHERE 1=1
    """
    params: list[Any] = []
    if channel and channel != "ALL":
        sql += " AND m.channel = ?"
        params.append(channel)
    if status and status != "ALL":
        sql += " AND m.status = ?"
        params.append(status)
    sql += " ORDER BY m.id LIMIT 300"
    with db.session(cfg.db_path) as conn:
        return _rows(conn, sql, tuple(params))


def pipeline_payload(stage: str | None, limit: int) -> dict:
    cfg = _cfg()
    with db.session(cfg.db_path) as conn:
        money = db.revenue(conn)
        rows = db.pipeline_rows(conn, stage, limit)
    return {
        "revenue": money,
        "stages": list(db.ALL_STAGES),
        "labels": STAGE_LABEL,
        "currency": cfg.get("pipeline.currency") or money.get("currency") or "",
        "deals": [dict(r) for r in rows],
    }


def save_message(message_id: int, subject: str | None, body: str) -> bool:
    """Edited drafts must keep the WhatsApp link in sync with the text.
    Returns False when no such message exists, so the API can say so."""
    from .senders.whatsapp import wa_link

    cfg = _cfg()
    with db.session(cfg.db_path) as conn:
        row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if not row:
            return False
        link = row["wa_link"]
        if row["channel"] == "whatsapp" and row["to_value"]:
            link = wa_link(row["to_value"], body)
        conn.execute(
            "UPDATE messages SET subject=?, body=?, wa_link=? WHERE id=?",
            (subject, body, link, message_id),
        )
    return True


# ------------------------------------------------------------------------ handler


class _BadRequest(ValueError):
    """Client sent something malformed; answered with a 400, never a traceback."""


def _as_int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise _BadRequest(f"{name} must be an integer") from None


def _int_list(values, name: str) -> list[int]:
    if not isinstance(values, list) or not values:
        raise _BadRequest(f"{name} must be a non-empty list of integers")
    return [_as_int(v, name) for v in values]


class Handler(BaseHTTPRequestHandler):
    server_version = "Prospector"

    def log_message(self, *args) -> None:  # keep the console clean for the log tail
        pass

    # -- auth ---------------------------------------------------------------

    def _authorized(self) -> bool:
        cookie = self.headers.get("Cookie") or ""
        if f"pt={TOKEN}" in cookie:
            return True
        if self.headers.get("X-Token") == TOKEN:
            return True
        query = parse_qs(urlparse(self.path).query)
        return query.get("token", [None])[0] == TOKEN

    def _same_origin(self) -> bool:
        """Block a malicious page from POSTing to this server behind your back."""
        origin = self.headers.get("Origin")
        if not origin:
            return True  # non-browser client (curl); token still required
        host = self.headers.get("Host", "")
        return origin.endswith(f"//{host}")

    # -- plumbing -----------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, default=str).encode(), "application/json")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- routes -------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            if not self._authorized():
                self._send(HTTPStatus.UNAUTHORIZED, b"Open the URL printed in your terminal.",
                           "text/plain")
                return
            html = (HERE / "webui.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8",
                       {"Set-Cookie": f"pt={TOKEN}; Path=/; SameSite=Strict"})
            return

        if not self._authorized():
            self._json({"error": "unauthorized"}, 401)
            return

        query = parse_qs(parsed.query)
        one = lambda k, d=None: query.get(k, [d])[0]

        try:
            self._handle_get(path, one)
        except _BadRequest as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            # A bad query must never kill the request thread with no response.
            self._json({"error": f"internal error: {type(exc).__name__}"}, 500)

    def _handle_get(self, path: str, one) -> None:
        if path == "/api/state":
            self._json(state_payload())
        elif path == "/api/job":
            self._json(JOB.snapshot())
        elif path == "/api/leads":
            limit = min(max(_as_int(one("limit", "300"), "limit"), 1), 2000)
            self._json(leads_payload(
                one("status", "ALL"), limit,
                category=one("category", "ALL"),
                day=one("day", "ALL"),
                since=one("since") or None,
            ))
        elif path == "/api/groups":
            self._json(groups_payload(
                one("status", "ALL"), one("category", "ALL"), one("since") or None
            ))
        elif path.startswith("/api/lead/"):
            detail = lead_detail(_as_int(path.rsplit("/", 1)[-1], "lead id"))
            self._json(detail or {"error": "not found"}, 200 if detail else 404)
        elif path == "/api/pipeline":
            limit = min(max(_as_int(one("limit", "300"), "limit"), 1), 2000)
            self._json(pipeline_payload(one("stage", "ALL"), limit))
        elif path == "/api/messages":
            self._json(messages_payload(one("channel", "ALL"), one("status", "ALL")))
        elif path == "/api/queue":
            out = ROOT / str(_cfg().get("whatsapp.queue_file", "out/whatsapp_queue.html"))
            if out.exists():
                self._send(200, out.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, b"Run the WhatsApp step first.", "text/plain")
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self._authorized():
            self._json({"error": "unauthorized"}, 401)
            return
        if not self._same_origin():
            self._json({"error": "cross-origin request refused"}, 403)
            return

        path = urlparse(self.path).path
        body = self._body()

        try:
            self._handle_post(path, body)
        except _BadRequest as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            self._json({"error": f"internal error: {type(exc).__name__}"}, 500)

    def _handle_post(self, path: str, body: dict) -> None:
        if True:
            if path == "/api/config":
                changed = update_config(body.get("updates") or {})
                self._json({"changed": changed, "state": state_payload()})

            elif path == "/api/keys":
                values = {k: v for k, v in (body.get("keys") or {}).items() if k in KNOWN_KEYS}
                self._json({"written": write_env_keys(values), "keys": read_env_keys()})

            elif path == "/api/run":
                self._json(self._run(body))

            elif path == "/api/stop":
                self._json({"stopped": JOB.stop()})

            elif path == "/api/lead-status":
                cfg = _cfg()
                status = body.get("status")
                if status not in db.STATUSES:
                    raise _BadRequest(f"status must be one of {', '.join(db.STATUSES)}")
                ids = _int_list(body.get("ids"), "ids")
                updated = 0
                with db.session(cfg.db_path) as conn:
                    for lead_id in ids:
                        if db.get_lead(conn, lead_id):
                            db.set_status(conn, lead_id, status,
                                          skip_reason=body.get("reason") if status == "SKIPPED" else None)
                            updated += 1
                self._json({"updated": updated, "unknown": len(ids) - updated})

            elif path == "/api/suppress":
                cfg = _cfg()
                value = (body.get("value") or "").strip()
                if not value:
                    self._json({"error": "nothing to suppress"}, 400)
                    return
                kind = body.get("kind") or ("email" if "@" in value else "phone")
                with db.session(cfg.db_path) as conn:
                    db.suppress(conn, value, kind, body.get("reason") or "via web UI")
                self._json({"suppressed": value, "kind": kind})

            elif path == "/api/stage":
                cfg = _cfg()
                stage = (body.get("stage") or "").upper()
                if stage not in db.ALL_STAGES:
                    raise _BadRequest(f"stage must be one of {', '.join(db.ALL_STAGES)}")
                ids = _int_list(body.get("ids"), "ids")
                amount = body.get("amount")
                if amount in ("", None):
                    amount = None
                else:
                    try:
                        amount = float(amount)
                    except (TypeError, ValueError):
                        raise _BadRequest("amount must be a number") from None
                updated = 0
                with db.session(cfg.db_path) as conn:
                    for lead_id in ids:
                        if not db.get_lead(conn, lead_id):
                            continue
                        db.set_stage(
                            conn, lead_id, stage,
                            amount=amount,
                            note=body.get("note") or None,
                            reason=body.get("reason") or None,
                            currency=body.get("currency")
                            or str(cfg.get("pipeline.currency", "") or "")
                            or None,
                        )
                        updated += 1
                self._json({"updated": updated, "unknown": len(ids) - updated})

            elif path == "/api/message":
                message_id = _as_int(body.get("id"), "id")
                if not (body.get("body") or "").strip():
                    raise _BadRequest("body must not be empty")
                saved = save_message(message_id, body.get("subject"), body["body"])
                if not saved:
                    raise _BadRequest(f"no message with id {message_id}")
                self._json({"saved": True})

            else:
                self._json({"error": "not found"}, 404)

    def _run(self, body: dict) -> dict:
        command = body.get("command")
        if command not in RUNNABLE:
            return {"ok": False, "message": f"unknown command {command!r}"}

        args: list[str] = [command]
        limit = body.get("limit")
        if limit:
            args += ["--limit", str(_as_int(limit, "limit"))]

        if command == "find":
            if body.get("niche"):
                args += ["--niche", str(body["niche"])]
            if body.get("location"):
                args += ["--location", str(body["location"])]
            if body.get("max"):
                args += ["--max", str(_as_int(body["max"], "max"))]
            if body.get("source") and body["source"] != "auto":
                args += ["--source", str(body["source"])]

        if command == "send":
            # The UI must ask for this explicitly; dry run is the default here
            # exactly as it is on the command line.
            if body.get("confirm") is True:
                args.append("--yes")

        if command == "wa":
            args.append("--no-open")

        ok, message = JOB.start(args)
        return {"ok": ok, "message": message, "args": args}


# --------------------------------------------------------------------------- main


def serve(port: int = 8787, open_browser: bool = True) -> None:
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        # Someone is already on this port — very often Prospector itself, left
        # running in another window. A stack trace helps nobody here.
        if exc.errno in (48, 98):  # EADDRINUSE on macOS / Linux
            print(
                f"\n  Port {port} is already in use.\n\n"
                f"  If Prospector is already running, open the link it printed —\n"
                f"  or look in out/ui.url. Otherwise start this one elsewhere:\n\n"
                f"      make ui PORT={port + 1}\n",
                flush=True,
            )
            raise SystemExit(2)
        raise
    url = f"http://127.0.0.1:{port}/?token={TOKEN}"
    # flush: this URL is the only way in, so it must appear even when stdout is
    # piped to a file or a launcher.
    banner = (
        "\n  Prospector UI\n\n"
        f"  {url}\n\n"
        "  Local only. Keep that link private — it unlocks your keys and sending.\n"
        "  Ctrl-C to stop.\n"
    )
    print(banner, flush=True)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="prospector.web", description="Local web UI")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    serve(port=args.port, open_browser=not args.no_open)
    return 0


if __name__ == "__main__":
    sys.exit(main())
