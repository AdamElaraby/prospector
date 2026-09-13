"""Message drafting.

One LLM call per lead per channel. The prompt is fed only facts that came out
of the pipeline — business name, category, review count, and the specific audit
finding — and is told in several ways not to invent anything else, because a
cold email containing a made-up detail about someone's business is worse than
no email.

Provider is config-driven (llm.provider: openai | anthropic) so swapping is a
one-line change.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from typing import Any

from . import db
from .audit import describe_issues
from .config import Config, env

SYSTEM_PROMPT = """You write short cold outreach messages for ONE PERSON who builds \
websites for local businesses. Not an agency, not a team, not a company \
— a single individual looking for small paid jobs.

You will be given verified facts about one local business. Write a message to \
that business.

Absolute rules:
- Use ONLY the facts provided. Never invent details, statistics, competitors, \
awards, customer counts, or claims about their business or their industry.
- Never use placeholder brackets like [Name] or [City]. If you do not know \
something, leave it out entirely.
- Do not claim to be a customer, to have been referred, or to have any prior \
relationship.
- Do not promise results, rankings, or revenue.
- Do not imply a company exists behind the sender. No "we", no "our team".
- Do not claim past clients, years of experience, or a portfolio unless one is \
explicitly given to you below.

Style rules:
{style_rules}

The ask at the end of the message: {call_to_action}

Return ONLY a JSON object, no markdown fence:
{output_shape}"""

EMAIL_SHAPE = '{"subject": "...", "body": "..."}'
WHATSAPP_SHAPE = '{"body": "..."}'

PLACEHOLDER_RE = re.compile(r"\[[A-Za-z ][A-Za-z _/-]{1,30}\]")

# The sender is one person; these phrases mean the model invented a company.
# Deliberately phrase-level, not a bare "we": "maybe we could talk" is the
# normal you-and-me plural and must stay legal.
COMPANY_VOICE_RE = re.compile(
    r"\b(?:we\s+at\b|our\s+(?:team|agency|company|studio|firm|clients|experts|designers|developers)"
    r"|on\s+behalf\s+of\s+our)\b",
    re.IGNORECASE,
)


class DraftError(RuntimeError):
    pass


# --------------------------------------------------------------------- prompts


def build_facts(lead: sqlite3.Row, audit: sqlite3.Row | None) -> str:
    lines = [f"Business name: {lead['name']}"]
    if lead["category"]:
        lines.append(f"Type of business: {lead['category']}")
    if lead["city"] or lead["address"]:
        lines.append(f"Location: {lead['city'] or lead['address']}")
    if lead["rating"] and lead["review_count"]:
        lines.append(
            f"Google rating: {lead['rating']} stars from {lead['review_count']} reviews"
        )

    if lead["lead_type"] == "NO_SITE":
        lines.append(
            "Web presence: they have a Google Business listing but NO website at all. "
            "Their Google listing is the only place customers can find them online."
        )
    else:
        lines.append(f"Current website: {lead['website']}")
        if audit is not None:
            problems = describe_issues(audit["issues_json"])
            if problems:
                lines.append("Verified problems with their current website:")
                lines.extend(f"  - {p}" for p in problems)
            if audit["load_ms"]:
                lines.append(f"  - homepage load time measured at {audit['load_ms']} ms")
    return "\n".join(lines)


def build_system_prompt(cfg: Config, channel: str) -> str:
    rules = cfg.get("llm.style_rules", []) or []
    shape = EMAIL_SHAPE if channel == "email" else WHATSAPP_SHAPE
    extra = []
    if channel == "email":
        extra.append(
            f"- Subject line: under {cfg.get('email.subject_max_chars', 60)} characters, "
            "lowercase is fine, no clickbait, no emoji, no 'Re:' fakery."
        )
    else:
        extra.append(
            "- This is a WhatsApp message. Under 55 words, no subject line, no "
            "formal sign-off, one or two short lines. It should read like a text."
        )
    rendered = "\n".join(f"- {r}" for r in rules) + "\n" + "\n".join(extra)
    return SYSTEM_PROMPT.format(
        style_rules=rendered,
        call_to_action=cfg.get(
            "llm.call_to_action", "ask if they want a free mockup"
        ),
        output_shape=shape,
    )


def build_user_prompt(cfg: Config, lead: sqlite3.Row, audit: sqlite3.Row | None) -> str:
    sender = cfg.get("sender", {}) or {}
    name = sender.get("name") or ""

    # A blank business name is the normal case, not a gap to paper over: the
    # sender is one person, and the message should sound like it.
    if sender.get("business_name"):
        who = f"You are writing as {name}, who builds websites under the name {sender['business_name']}."
    else:
        who = (
            f"You are writing as {name}, one person who builds websites for local "
            "businesses. There is no company. Do not invent one."
        )

    parts = [who]
    if sender.get("portfolio_url"):
        parts.append(f"Work you may link once: {sender['portfolio_url']}")
    else:
        parts.append("No portfolio link is available. Do not reference past work.")
    if sender.get("price"):
        parts.append(
            f"State the price plainly, once: {sender['price']}. Do not pad it with "
            "justification."
        )
    parts.append("\nFacts about the business you are writing to:\n")
    parts.append(build_facts(lead, audit))
    parts.append("\nWrite the message now. Sign off with the first name only.")
    return "\n".join(parts)


# ------------------------------------------------------------------- providers


# Errors worth waiting out rather than giving up on. A rate limit or a brief
# provider blip should cost a few seconds, not the whole batch.
TRANSIENT_MARKERS = (
    "rate limit", "ratelimit", "429", "timeout", "timed out", "connection",
    "temporarily unavailable", "overloaded", "502", "503", "504",
    "internalservererror", "apiconnection",
)


def _is_transient(exc: Exception) -> bool:
    blob = f"{type(exc).__name__} {exc}".lower()
    if "insufficient_quota" in blob or "invalid_api_key" in blob:
        return False  # retrying a billing or key problem just wastes time
    return any(marker in blob for marker in TRANSIENT_MARKERS)


def complete(cfg: Config, system: str, user: str) -> str:
    provider = (cfg.get("llm.provider", "openai") or "openai").lower()
    model = cfg.get("llm.model", "gpt-4o-mini")
    temperature = float(cfg.get("llm.temperature", 0.7))
    attempts = max(1, int(cfg.get("llm.max_retries", 4)))
    backoff = float(cfg.get("llm.retry_base_seconds", 2))

    if provider == "openai":
        call = lambda: _openai(model, system, user, temperature)
    elif provider == "anthropic":
        call = lambda: _anthropic(model, system, user, temperature)
    else:
        raise DraftError(f"Unknown llm.provider '{provider}'. Use openai or anthropic.")

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return call()
        except DraftError as exc:
            last = exc
            cause = exc.__cause__ or exc
            if attempt == attempts - 1 or not _is_transient(cause):
                raise
            time.sleep(backoff * (2 ** attempt))
    raise last if last else DraftError("drafting failed for an unknown reason")


def _openai(model: str, system: str, user: str, temperature: float) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:
        # Report what actually broke. "pip install openai" as a blanket message
        # hides a wrong interpreter, a half-finished install, and a genuine
        # dependency conflict behind the same three words.
        raise DraftError(
            f"can't import the openai library ({exc}). "
            f"Running: {sys.executable} — if that is not the project's .venv, "
            "use it, or run: .venv/bin/pip install -r requirements.txt"
        ) from exc

    client = OpenAI(api_key=env("OPENAI_API_KEY", required=True))
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        # Rate limits, bad keys, unknown models and billing problems all land
        # here; each needs a different fix, so pass the provider's words along.
        raise DraftError(f"{type(exc).__name__} from OpenAI: {exc}") from exc
    return resp.choices[0].message.content or ""


def _anthropic(model: str, system: str, user: str, temperature: float) -> str:
    try:
        import anthropic
    except ImportError as exc:
        raise DraftError(
            f"can't import the anthropic library ({exc}). "
            f"Running: {sys.executable}. Install it with: .venv/bin/pip install anthropic"
        ) from exc

    client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY", required=True))
    resp = client.messages.create(
        model=model,
        max_tokens=600,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(block.text for block in resp.content if block.type == "text")


# ---------------------------------------------------------------------- public


def draft_message(
    cfg: Config, lead: sqlite3.Row, audit: sqlite3.Row | None, channel: str
) -> dict[str, str]:
    raw = complete(
        cfg,
        build_system_prompt(cfg, channel),
        build_user_prompt(cfg, lead, audit),
    )
    data = _parse_json(raw)

    body = (data.get("body") or "").strip()
    subject = (data.get("subject") or "").strip()
    if not body:
        raise DraftError(f"Model returned no body for lead {lead['id']}")

    problems = _validate(body, subject, channel, cfg)
    if problems:
        raise DraftError("; ".join(problems))

    return {"subject": subject, "body": body}


def _parse_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n|\n```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    raise DraftError(f"Model did not return JSON: {raw[:200]}")


def _validate(body: str, subject: str, channel: str, cfg: Config) -> list[str]:
    problems = []
    if PLACEHOLDER_RE.search(body) or PLACEHOLDER_RE.search(subject):
        problems.append("contains an unfilled [placeholder]")
    match = COMPANY_VOICE_RE.search(body) or COMPANY_VOICE_RE.search(subject)
    if match:
        problems.append(f"speaks as a company ({match.group(0)!r}) — the sender is one person")
    if channel == "email":
        if not subject:
            problems.append("no subject line")
        max_chars = int(cfg.get("email.subject_max_chars", 60))
        if len(subject) > max_chars + 15:
            problems.append(f"subject is {len(subject)} chars, over the {max_chars} limit")
        if len(body.split()) > 130:
            problems.append("body is far longer than the 70-word target")
    else:
        if len(body.split()) > 110:
            problems.append("WhatsApp body is too long")
    return problems


def render_email(cfg: Config, body: str) -> tuple[str, str]:
    """Attach the legally required footer. Returns (text, html)."""
    sender = cfg.get("sender", {}) or {}
    footer_lines = [
        cfg.get("email.unsubscribe_line", "Reply STOP and I won't email again."),
        sender.get("business_name") or sender.get("name") or "",
        cfg.get("email_channel.postal_address", ""),
    ]
    footer = "\n".join(line for line in footer_lines if line)
    text = f"{body.strip()}\n\n--\n{footer}"

    body_html = "".join(
        f"<p style='margin:0 0 12px'>{_escape(p)}</p>"
        for p in body.strip().split("\n\n")
    )
    footer_html = "<br>".join(_escape(line) for line in footer_lines if line)
    html = (
        "<div style=\"font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
        "font-size:15px;line-height:1.55;color:#111\">"
        f"{body_html}"
        "<hr style='border:none;border-top:1px solid #ddd;margin:20px 0 10px'>"
        f"<div style='font-size:12px;color:#777'>{footer_html}</div>"
        "</div>"
    )
    return text, html


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>")
    )


def pick_channel(conn: sqlite3.Connection, lead: sqlite3.Row, cfg: Config | None = None) -> str:
    """A business with no website has no email to find, so it goes to WhatsApp.

    This is the whole reason both channels exist: the highest-intent leads are
    exactly the ones email cannot reach. Until email is switched on and set up,
    everything goes to WhatsApp, which needs no domain and no address.
    """
    email_on = bool(cfg and cfg.email_enabled and not cfg.check_email_ready())
    if email_on and db.best_email(conn, lead["id"]):
        return "email"
    if lead["phone"]:
        return "whatsapp"
    return "none"
