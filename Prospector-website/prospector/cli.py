"""Command line entry point. Each stage is its own command and each one is
safe to re-run: state lives in SQLite, so nothing is discovered, drafted, or
sent twice.

    python -m prospector.cli find
    python -m prospector.cli audit
    python -m prospector.cli draft
    python -m prospector.cli send            # dry run
    python -m prospector.cli send --yes      # actually sends
    python -m prospector.cli wa
"""

from __future__ import annotations

# Must run before any dependency import: a Rosetta shell launches the x86_64
# slice of the universal Python, and every native wheel then fails to load.
from prospector._arch import ensure_native

ensure_native("prospector.cli")

import argparse
import csv
import os
import re
import sqlite3
import sys
import time
import webbrowser
from pathlib import Path


def _reexec_in_venv() -> None:
    """Re-run under the project's own interpreter if this one lacks the deps.

    Typing `python3 -m prospector.cli` is the natural thing to do and fails with
    a bare ModuleNotFoundError that says nothing about virtualenvs. Rather than
    lecture, just switch to the interpreter that has the dependencies.
    """
    if os.environ.get("PROSPECTOR_REEXEC"):
        return  # already switched once; a second attempt would loop
    venv_dir = Path(__file__).resolve().parent.parent / ".venv"
    venv_python = venv_dir / "bin" / "python"
    if not venv_python.is_file():
        return
    # Compare sys.prefix, not the executable path: a venv's bin/python is a
    # symlink to the base interpreter, so resolving both makes an outside
    # interpreter look identical to the inside one.
    if sys.prefix == str(venv_dir):
        return
    os.environ["PROSPECTOR_REEXEC"] = "1"
    os.execv(str(venv_python), [str(venv_python), "-m", "prospector.cli", *sys.argv[1:]])


try:
    from rich.console import Console
except ModuleNotFoundError:
    _reexec_in_venv()
    print(
        "Dependencies are missing and no .venv was found.\n"
        "From the project folder run:  make setup",
        file=sys.stderr,
    )
    raise SystemExit(1)

from rich.console import Console
from rich.table import Table

from . import db, draft as drafting, qualify
from .audit import Auditor, describe_issues, domain_of
from .opportunity import describe_gaps
from .progress import Progress
from .config import ConfigError, load_config, out_dir
from .countries import line_type
from .senders.whatsapp import normalize_phone, render_queue, wa_link

console = Console()


def _edit_target(cfg) -> str:
    """The file a user should edit: the private local config, never the public
    template — telling people to edit config.yaml is how personal details end up
    in a commit."""
    from .config import TEMPLATE_CONFIG

    if cfg.path.resolve() == TEMPLATE_CONFIG.resolve():
        return "config.local.yaml (create it first: cp config.yaml config.local.yaml)"
    return cfg.path.name


# ============================================================ find


def cmd_find(args, cfg) -> None:
    from .sources import get_source

    niches = [args.niche] if args.niche else cfg.require("search.niches")
    locations = [args.location] if args.location else cfg.require("search.locations")
    max_results = args.max or int(cfg.get("search.max_results_per_query", 60))

    source = get_source(cfg, args.source)
    if hasattr(source, "headless") and args.headful:
        source.headless = False
    console.print(f"[dim]source:[/dim] {source.name}")

    created = skipped = failed = 0
    # Each (niche x location) pair is one unit of work, and the count is known
    # before anything runs — so this bar is a real fraction, not a guess.
    pairs = [(n, l) for n in niches for l in locations]
    bar = Progress("finding businesses", len(pairs))

    with db.session(cfg.db_path) as conn:
        for niche, location in pairs:
            console.print(f"[bold]searching[/bold] {niche} in {location}")
            bar.advance(0, note=f"{niche} in {location}")
            try:
                results = list(source.search(niche, location, max_results))
            except Exception as exc:
                failed += 1
                console.print(f"  [red]failed:[/red] {exc}")
                bar.advance(1, note=f"{niche} in {location}: failed")
                continue

            for lead in results:
                if lead.get("business_status") == "CLOSED_PERMANENTLY":
                    continue
                lead.pop("business_status", None)
                # Some sources harvest emails during discovery; keep them
                # rather than making the audit re-find the same addresses.
                emails = lead.pop("emails", None) or []
                lead_id, is_new = db.upsert_lead(conn, lead)
                for address in emails:
                    db.add_contact(conn, lead_id, "email", address, "discovery")
                if is_new:
                    created += 1
                else:
                    skipped += 1
            console.print(f"  found {len(results)}")
            bar.advance(1, note=f"{niche} in {location}: {len(results)} found")

    summary = f"{created} new"
    if failed:
        summary += f", {failed} of {len(pairs)} searches failed"
    bar.finish(summary)
    console.print(f"\n[green]{created} new[/green], {skipped} already known")
    if failed:
        console.print(f"[red]{failed} of {len(pairs)} searches failed.[/red] See the errors above.")
        raise SystemExit(1)
    if created:
        console.print("next: [bold]python -m prospector.cli audit[/bold]")


# ============================================================ audit


def cmd_audit(args, cfg) -> None:
    """Check each site, then decide whether the business is worth pitching.

    Two separate judgements, in this order and never merged:

      1. What is wrong with the site — technical defects (audit.py) and missing
         commercial capabilities (opportunity.py), recorded apart from each
         other because they are different kinds of claim.
      2. Whether this is a lead — scored in qualify.py from activity, the
         demonstrated problem, plausible ability to pay, and whether one person
         could realistically deliver it.

    Having no website only wins step 1. A business still has to clear step 2.
    """
    include_no_site = bool(cfg.get("qualify.include_no_site", True))
    include_bad_site = bool(cfg.get("qualify.include_bad_site", True))
    min_reviews = int(cfg.get("qualify.min_review_count", 0))
    exclusions = [p.lower() for p in cfg.get("qualify.exclude_name_patterns", []) or []]

    with db.session(cfg.db_path) as conn:
        leads = db.leads_by_status(conn, "NEW", args.limit)
        if not leads:
            console.print("nothing with status NEW. Run [bold]find[/bold] first.")
            return

        console.print(f"auditing {len(leads)} leads")
        bar = Progress("auditing websites", len(leads))
        qualified = skipped = 0

        with Auditor(cfg) as auditor:
            for lead in leads:
                bar.advance(0, note=lead["name"])
                reason = _prefilter(lead, exclusions, min_reviews)
                if reason:
                    db.set_status(conn, lead["id"], "SKIPPED", skip_reason=reason, qualified=False)
                    skipped += 1
                    console.print(f"  [dim]skip[/dim] {lead['name']} — {reason}")
                    bar.advance(1, note=lead["name"])
                    continue

                result = auditor.audit(lead["website"])
                emails = result.pop("emails", [])
                result.pop("gaps", None)  # list form, for the caller; not a column
                db.save_audit(conn, lead["id"], result)
                for item in emails:
                    db.add_contact(conn, lead["id"], "email", item["email"], item["found_on"])

                supp = db.is_suppressed(conn, db.best_email(conn, lead["id"]), lead["phone"])
                if supp:
                    db.set_status(conn, lead["id"], "SKIPPED", skip_reason=supp, qualified=False)
                    skipped += 1
                    bar.advance(1, note=lead["name"])
                    continue

                # The channel switches are a campaign decision, not a judgement
                # about the business, so they are applied before scoring.
                has_site = bool(lead["website"])
                if not has_site and not include_no_site:
                    db.set_status(conn, lead["id"], "SKIPPED", qualified=False,
                                  skip_reason="no-site leads are switched off")
                    skipped += 1
                    bar.advance(1, note=lead["name"])
                    continue
                if has_site and not include_bad_site:
                    db.set_status(conn, lead["id"], "SKIPPED", qualified=False,
                                  skip_reason="weak-site leads are switched off")
                    skipped += 1
                    bar.advance(1, note=lead["name"])
                    continue

                lead_type = "NO_SITE" if not has_site else "BAD_SITE"
                # qualify reads lead_type, and the row in hand predates it.
                scored_lead = dict(lead)
                scored_lead["lead_type"] = lead_type
                verdict = qualify.score_lead(
                    scored_lead, db.get_audit(conn, lead["id"]), cfg
                )

                common = {
                    "lead_score": verdict["total"],
                    "lead_grade": verdict["grade"],
                    "score_json": qualify.to_json(verdict),
                }
                if verdict["qualified"]:
                    db.set_status(conn, lead["id"], "AUDITED", qualified=True,
                                  lead_type=lead_type, **common)
                    qualified += 1
                    headline = (verdict["reasons"] or ["qualified"])[0]
                    console.print(
                        f"  [green]{verdict['grade']}[/green] {lead['name']} "
                        f"[dim]{verdict['total']}/100[/dim] — {headline}"
                    )
                else:
                    db.set_status(conn, lead["id"], "SKIPPED", qualified=False,
                                  lead_type=lead_type,
                                  skip_reason=verdict["skip_reason"], **common)
                    skipped += 1
                    console.print(
                        f"  [dim]skip[/dim] {lead['name']} [dim]{verdict['total']}/100[/dim] "
                        f"— {verdict['skip_reason']}"
                    )
                bar.advance(1, note=lead["name"])
                conn.commit()  # auditing is slow; an interrupt must not undo it

        bar.finish(f"{qualified} qualified")

    console.print(f"\n[green]{qualified} qualified[/green], {skipped} skipped")
    if qualified:
        console.print("next: [bold]python -m prospector.cli draft[/bold]")


def _prefilter(lead: sqlite3.Row, exclusions: list[str], min_reviews: int) -> str | None:
    """Cheap structural filters, applied before an HTTP request is spent.

    Scoring judgements live in qualify.py; this is only for leads there is no
    point looking at in the first place.
    """
    name = (lead["name"] or "").lower()
    for pattern in exclusions:
        if pattern in name:
            return f"excluded by name pattern '{pattern}'"
    if min_reviews and lead["review_count"] is not None and lead["review_count"] < min_reviews:
        return f"only {lead['review_count']} reviews"
    return None


# ============================================================ draft


def cmd_draft(args, cfg) -> None:
    if cfg.check_sender_ready():
        console.print(
            f"[red]Set your name first.[/red] In {_edit_target(cfg)}, change "
            "[bold]sender.name[/bold] from \"Your Name\" to yours. That is the only "
            "thing drafting needs."
        )
        raise SystemExit(1)

    from .countries import region_for_location

    fallback_region = str(cfg.get("whatsapp.default_region", "") or "").upper() or None
    wa_enabled = bool(cfg.get("whatsapp.enabled", True))

    with db.session(cfg.db_path) as conn:
        leads = [l for l in db.leads_by_status(conn, "AUDITED", args.limit) if l["qualified"]]
        if not leads:
            console.print("nothing to draft. Run [bold]audit[/bold] first.")
            return

        # Best-scored first: if the batch is cut short by a limit, a rate
        # limit or an interrupt, the leads you paid to draft are the good ones.
        leads.sort(key=lambda l: -(l["lead_score"] or 0))
        console.print(f"drafting for {len(leads)} leads via {cfg.get('llm.provider')}/{cfg.get('llm.model')}")
        bar = Progress("writing messages", len(leads))
        done = failed = 0

        for index, lead in enumerate(leads):
            # Advanced up front rather than at each exit: this loop has eight
            # `continue`s, and a bar that only moves on the happy path stalls.
            bar.advance(1 if index else 0, note=lead["name"])
            supp = db.is_suppressed(conn, db.best_email(conn, lead["id"]), lead["phone"])
            if supp:
                db.set_status(conn, lead["id"], "SKIPPED", skip_reason=supp, qualified=False)
                console.print(f"  [dim]skip[/dim] {lead['name']} — {supp}")
                continue

            channel = (
                args.channel if args.channel != "auto"
                else drafting.pick_channel(conn, lead, cfg)
            )
            if channel == "email" and not db.best_email(conn, lead["id"]):
                # Forcing the channel doesn't conjure an address; bail before
                # paying for a draft that can never be delivered.
                console.print(
                    f"  [dim]skip[/dim] {lead['name']} — no email address on file"
                )
                continue
            if channel == "whatsapp" and not lead["phone"]:
                console.print(f"  [dim]skip[/dim] {lead['name']} — no phone number on file")
                continue
            if channel == "whatsapp" and not wa_enabled:
                channel = "none"
            if channel == "none":
                has_email = bool(db.best_email(conn, lead["id"]))
                if has_email:
                    reason = (
                        "has an email address, but the email channel is "
                        + ("not fully configured" if cfg.email_enabled else "off")
                        + " — enable it in Setup to reach them"
                    )
                else:
                    reason = "no email and no phone"
                db.set_status(conn, lead["id"], "SKIPPED", skip_reason=reason)
                console.print(f"  [dim]skip[/dim] {lead['name']} — {reason}")
                continue

            audit_row = db.get_audit(conn, lead["id"])
            try:
                message = drafting.draft_message(cfg, lead, audit_row, channel)
            except Exception as exc:
                failed += 1
                console.print(f"  [red]fail[/red] {lead['name']} — {exc}")
                continue

            if channel == "email":
                to = db.best_email(conn, lead["id"])
                db.save_message(conn, lead["id"], "email", message["body"],
                                subject=message["subject"], to_value=to)
            else:
                # Google's formatted address ends in the country, so it beats
                # the search string; the search string beats the bare city.
                lead_region = region_for_location(
                    lead["address"], lead["search_location"], lead["city"]
                ) or fallback_region
                if not lead_region:
                    db.set_status(
                        conn, lead["id"], "SKIPPED",
                        skip_reason=f"can't tell which country '{lead['city'] or lead['search_location']}' is in",
                    )
                    console.print(f"  [dim]skip[/dim] {lead['name']} — unknown country, can't dial safely")
                    continue

                phone = normalize_phone(lead["phone"], lead_region)
                if not phone:
                    db.set_status(conn, lead["id"], "SKIPPED",
                                  skip_reason=f"not a valid {lead_region} phone number: {lead['phone']}")
                    console.print(
                        f"  [dim]skip[/dim] {lead['name']} — {lead['phone']!r} isn't a "
                        f"valid {lead_region} number"
                    )
                    continue
                db.save_message(conn, lead["id"], "whatsapp", message["body"],
                                to_value=phone, wa_link=wa_link(phone, message["body"]))

            db.set_status(conn, lead["id"], "DRAFTED")
            # A draft you can read is a lead you have reviewed enough to judge.
            db.set_stage(conn, lead["id"], "REVIEWED", advance_only=True)
            conn.commit()  # each draft is money spent; an interrupt must not undo it
            done += 1
            console.print(f"  [green]✓[/green] {lead['name']} [dim]({channel})[/dim]")

        bar.finish(f"{done} drafted")

    console.print(f"\n[green]{done} drafted[/green]" + (f", [red]{failed} failed[/red]" if failed else ""))
    console.print("next: [bold]preview[/bold], then [bold]send[/bold] / [bold]wa[/bold]")
    if failed and not done:
        raise SystemExit(1)  # every draft failed: make scripts and the web UI see it


# ============================================================ preview


def cmd_preview(args, cfg) -> None:
    with db.session(cfg.db_path) as conn:
        rows = conn.execute(
            """
            SELECT m.*, l.name AS business_name, l.lead_type, l.website
            FROM messages m JOIN leads l ON l.id = m.lead_id
            WHERE m.status = 'DRAFT' ORDER BY m.id LIMIT ?
            """,
            (args.limit or 10,),
        ).fetchall()

    if not rows:
        console.print("no drafts waiting.")
        return

    for row in rows:
        console.rule(f"[bold]#{row['lead_id']} {row['business_name']}[/bold] · {row['channel']}")
        if row["channel"] == "email":
            console.print(f"[dim]to:[/dim]      {row['to_value']}")
            console.print(f"[dim]subject:[/dim] {row['subject']}")
        else:
            console.print(f"[dim]to:[/dim]      +{row['to_value']}")
        console.print()
        console.print(row["body"])
        console.print()


# ============================================================ send (email)


def cmd_send(args, cfg) -> None:
    from .senders.email_resend import ResendSender, SendError

    if not cfg.email_enabled:
        console.print(
            "[yellow]Email is off.[/yellow] It needs a domain you own and a real "
            "postal address (CAN-SPAM applies to individuals too).\n"
            f"Turn it on with [bold]email_channel.enabled: true[/bold] in {_edit_target(cfg)}, "
            "or just use [bold]wa[/bold] — WhatsApp needs neither."
        )
        return
    missing = cfg.check_sender_ready() + cfg.check_email_ready()
    if missing:
        console.print("[red]Email still needs:[/red] " + ", ".join(missing))
        return

    limit = args.limit or int(cfg.get("email.default_batch_size", 25))
    delay = float(cfg.get("email.delay_seconds", 8))

    with db.session(cfg.db_path) as conn:
        pending = db.pending_messages(conn, "email", limit)
        if not pending:
            console.print("no email drafts waiting.")
            return

        # Filter before the dry run, not just before the send, so what you are
        # shown is exactly what would go out.
        sendable, blocked = [], []
        for row in pending:
            reason = None
            if not row["to_value"]:
                reason = "no recipient address"
            else:
                reason = db.is_suppressed(conn, row["to_value"], None)
            (blocked if reason else sendable).append((row, reason))

        for row, reason in blocked:
            db.mark_message_failed(conn, row["id"], reason)
            db.set_status(conn, row["lead_id"], "SKIPPED", skip_reason=reason)
            console.print(f"  [dim]blocked[/dim] {row['business_name']} — {reason}")

        if not sendable:
            console.print("nothing left to send after the suppression list.")
            return

        if not args.yes:
            console.print(f"[yellow]DRY RUN[/yellow] — {len(sendable)} emails would be sent:\n")
            table = Table(show_header=True, header_style="bold")
            table.add_column("lead")
            table.add_column("to")
            table.add_column("subject")
            for row, _ in sendable:
                table.add_row(row["business_name"], row["to_value"], row["subject"] or "—")
            console.print(table)
            console.print(
                f"\nRun [bold]send --yes[/bold] to actually send these "
                f"{len(sendable)} emails from {cfg.get('email_channel.from_email')}."
            )
            return

        try:
            sender = ResendSender(cfg)
        except Exception as exc:
            console.print(f"[red]{exc}[/red]")
            return

        console.print(f"sending {len(sendable)} emails as {sender.from_header}")
        bar = Progress("sending emails", len(sendable))
        sent = failed = 0

        for index, (row, _) in enumerate(sendable, 1):
            to = row["to_value"]
            bar.advance(0, note=row["business_name"])
            text, html_body = drafting.render_email(cfg, row["body"])
            try:
                provider_id = sender.send(to, row["subject"] or "", text, html_body)
                db.mark_message_sent(conn, row["id"], provider_id)
                db.set_status(conn, row["lead_id"], "SENT")
                # A message delivered is an attempt at a conversation. It is
                # not a reply, and the funnel never treats it as one.
                db.set_stage(conn, row["lead_id"], "CONTACTED", advance_only=True)
                sent += 1
                console.print(f"  [green]sent[/green] {row['business_name']} → {to}")
            except SendError as exc:
                db.mark_message_failed(conn, row["id"], str(exc))
                db.set_status(conn, row["lead_id"], "FAILED")
                failed += 1
                console.print(f"  [red]fail[/red] {row['business_name']} — {exc}")

            conn.commit()
            bar.advance(1, note=row["business_name"])
            if index < len(sendable) and delay:
                time.sleep(delay)

        bar.finish(f"{sent} sent")

    console.print(f"\n[green]{sent} sent[/green]" + (f", [red]{failed} failed[/red]" if failed else ""))


# ============================================================ wa queue


def cmd_wa(args, cfg) -> None:
    with db.session(cfg.db_path) as conn:
        pending = db.pending_messages(conn, "whatsapp", args.limit)
        if not pending:
            console.print("no WhatsApp drafts waiting.")
            return

        bar = Progress("building the WhatsApp queue", len(pending))
        rows = []
        for row in pending:
            bar.advance(1)
            lead = db.get_lead(conn, row["lead_id"])
            # A suppression added after drafting must still keep the number out
            # of the queue — same rule the email path applies before sending.
            supp = db.is_suppressed(conn, None, row["to_value"] or lead["phone"])
            if supp:
                db.mark_message_failed(conn, row["id"], f"suppressed: {supp}")
                db.set_status(conn, lead["id"], "SKIPPED", skip_reason=supp)
                console.print(f"  [dim]removed[/dim] {lead['name']} — {supp}")
                continue
            rows.append(
                {
                    "lead_id": row["lead_id"],
                    "name": lead["name"],
                    "category": lead["category"],
                    "city": lead["city"],
                    "rating": lead["rating"],
                    "review_count": lead["review_count"],
                    "phone": "+" + (row["to_value"] or ""),
                    "wa_link": row["wa_link"],
                    "body": row["body"],
                    "lead_type": lead["lead_type"],
                    "line_type": line_type(row["to_value"]),
                }
            )

    target = Path(cfg.get("whatsapp.queue_file", "out/whatsapp_queue.html"))
    if not target.is_absolute():
        # Relative to the config file that is driving this run, so a second
        # config (a test one, a second campaign) can never overwrite the
        # queue belonging to a different one.
        target = cfg.path.parent / target
    path = render_queue(rows, target, command_prefix=_cli_invocation())
    bar.finish(f"{len(rows)} in the queue")
    landline = sum(1 for r in rows if r["line_type"] == "landline")
    console.print(f"[green]{len(rows)} leads[/green] written to {path}")
    if landline:
        console.print(
            f"  [yellow]{landline}[/yellow] are landlines and almost certainly not on "
            f"WhatsApp — they're listed last and marked. {len(rows) - landline} are mobiles."
        )
    console.print("Open it, send each one by hand, then run the mark-sent command it gives you.")
    if not args.no_open:
        webbrowser.open(path.as_uri())


def _cli_invocation() -> str:
    """How the user is actually running this, e.g. '.venv/bin/python -m prospector.cli'."""
    from .config import ROOT

    exe = Path(sys.executable)
    try:
        exe_str = str(exe.relative_to(ROOT))
    except ValueError:
        exe_str = exe.name
    return f"{exe_str} -m prospector.cli"


# ============================================================ bookkeeping


def cmd_mark_sent(args, cfg) -> None:
    """Ids come from a hand-copied command, so a typo must be loud: silently
    'succeeding' on a wrong id buries a live lead and un-counts a real send."""
    done, problems = [], []
    with db.session(cfg.db_path) as conn:
        for lead_id in args.ids:
            lead = db.get_lead(conn, lead_id)
            if not lead:
                problems.append(f"#{lead_id}: no such lead — check for a typo")
                continue
            if lead["status"] == "REPLIED":
                problems.append(f"#{lead_id} {lead['name']}: already REPLIED, left alone")
                continue
            row = conn.execute(
                "SELECT id FROM messages WHERE lead_id = ? AND channel = 'whatsapp'",
                (lead_id,),
            ).fetchone()
            if not row:
                problems.append(
                    f"#{lead_id} {lead['name']}: has no WhatsApp draft — nothing to mark"
                )
                continue
            db.mark_message_sent(conn, row["id"], None)
            db.set_status(conn, lead_id, "SENT")
            db.set_stage(conn, lead_id, "CONTACTED", advance_only=True)
            done.append(lead_id)
    if done:
        console.print(f"[green]marked {len(done)} sent[/green]")
    for problem in problems:
        console.print(f"  [yellow]skipped[/yellow] {problem}")


def cmd_mark_replied(args, cfg) -> None:
    done, unknown = [], []
    with db.session(cfg.db_path) as conn:
        for lead_id in args.ids:
            if db.get_lead(conn, lead_id):
                db.set_status(conn, lead_id, "REPLIED")
                # A reply proves contact happened. Whether they then asked for
                # a proposal is a separate fact, recorded with `pipeline`.
                db.set_stage(conn, lead_id, "CONTACTED", advance_only=True)
                done.append(lead_id)
            else:
                unknown.append(lead_id)
    if done:
        console.print(
            f"[green]marked {len(done)} replied[/green] — they will never be contacted again"
        )
        console.print(
            "  record what the conversation does next: "
            "[bold]pipeline proposal-requested / quoted / won[/bold]"
        )
    for lead_id in unknown:
        console.print(f"  [yellow]skipped[/yellow] #{lead_id}: no such lead — check for a typo")


def cmd_suppress(args, cfg) -> None:
    with db.session(cfg.db_path) as conn:
        for value in args.values:
            kind = args.kind
            if kind == "auto":
                kind = "email" if "@" in value else ("phone" if re.search(r"\d", value) else "domain")
            db.suppress(conn, value, kind, args.reason)
            console.print(f"suppressed [bold]{value}[/bold] ({kind})")


# ============================================================ sales pipeline


# What you type -> what is stored. Hyphens because nobody types underscores.
STAGE_ALIASES = {
    "reviewed": "REVIEWED",
    "contacted": "CONTACTED",
    "proposal": "PROPOSAL_REQUESTED",
    "proposal-requested": "PROPOSAL_REQUESTED",
    "quoted": "QUOTED",
    "quote": "QUOTED",
    "won": "WON",
    "lost": "LOST",
}

STAGE_LABEL = {
    "REVIEWED": "reviewed",
    "CONTACTED": "conversation started",
    "PROPOSAL_REQUESTED": "proposal requested",
    "QUOTED": "quote sent",
    "WON": "won",
    "LOST": "lost",
}


def cmd_pipeline(args, cfg) -> None:
    """Record what actually happened with a lead, as opposed to what was sent.

    This is the only place real revenue is entered, and it is entered by hand
    on purpose: nothing the machine can observe tells it a deal closed.
    """
    stage = STAGE_ALIASES.get(args.stage.lower().replace("_", "-"))
    if not stage:
        console.print(
            f"[red]unknown stage '{args.stage}'[/red]. Use one of: "
            + ", ".join(sorted(STAGE_ALIASES))
        )
        raise SystemExit(1)

    if stage in ("QUOTED", "WON") and args.amount is None:
        verb = "quoted" if stage == "QUOTED" else "won"
        console.print(
            f"[yellow]No amount given.[/yellow] Recording a {verb} deal without one "
            "makes the revenue figure a lie by omission. Add [bold]--amount 450[/bold]."
        )

    currency = args.currency or str(cfg.get("pipeline.currency", "") or "") or None
    updated, unknown = [], []
    with db.session(cfg.db_path) as conn:
        for lead_id in args.ids:
            lead = db.get_lead(conn, lead_id)
            if not lead:
                unknown.append(lead_id)
                continue
            db.set_stage(
                conn, lead_id, stage,
                amount=args.amount, note=args.note, reason=args.reason,
                currency=currency,
            )
            updated.append((lead_id, lead["name"]))

    for lead_id, name in updated:
        money = f" ({_money(args.amount, currency)})" if args.amount is not None else ""
        console.print(f"  [green]{STAGE_LABEL[stage]}[/green] #{lead_id} {name}{money}")
    if updated:
        console.print(f"\n[green]{len(updated)} updated[/green]")
    for lead_id in unknown:
        console.print(f"  [yellow]skipped[/yellow] #{lead_id}: no such lead — check for a typo")


def cmd_funnel(args, cfg) -> None:
    with db.session(cfg.db_path) as conn:
        money = db.revenue(conn)
        rows = db.pipeline_rows(conn, args.stage.upper() if args.stage else None, args.limit or 50)
    _print_funnel(money)

    if not rows:
        console.print(
            "\nNothing recorded yet. As conversations happen, log them:\n"
            "  [bold]pipeline contacted 12[/bold]\n"
            "  [bold]pipeline quoted 12 --amount 450[/bold]\n"
            "  [bold]pipeline won 12 --amount 450[/bold]"
        )
        return

    table = Table(show_header=True, header_style="bold", title="deals")
    for col in ("id", "business", "stage", "quoted", "won", "updated"):
        table.add_column(col)
    currency = money["currency"]
    for row in rows:
        table.add_row(
            str(row["lead_id"]),
            (row["name"] or "")[:30],
            STAGE_LABEL.get(row["stage"], row["stage"]),
            _money(row["quote_amount"], currency),
            _money(row["won_amount"], currency),
            (row["updated_at"] or "")[:10],
        )
    console.print(table)


def _print_funnel(money: dict) -> None:
    steps = money["funnel"]
    table = Table(title="sales pipeline", show_header=True, header_style="bold")
    table.add_column("stage")
    table.add_column("reached", justify="right")
    table.add_column("of previous", justify="right")

    previous = None
    for stage in db.PIPELINE_STAGES:
        n = steps.get(stage, 0)
        # Conversion against the stage before it, which is the number that
        # tells you where the process is actually leaking.
        rate = f"{n / previous * 100:.0f}%" if previous else "—"
        table.add_row(STAGE_LABEL[stage], str(n), rate)
        previous = n or None
    if steps.get("LOST"):
        table.add_row("[dim]lost[/dim]", f"[dim]{steps['LOST']}[/dim]", "")
    console.print(table)

    currency = money["currency"]
    console.print(
        f"\n[bold]won[/bold] {money['won_count']} deals, "
        f"{_money(money['won_total'], currency)}"
        + (f" · average {_money(money['avg_deal'], currency)}" if money["won_count"] else "")
    )
    if money["open_count"]:
        console.print(
            f"[bold]open[/bold] {money['open_count']} quotes out, "
            f"{_money(money['open_total'], currency)} still live"
        )
    if steps.get("CONTACTED"):
        console.print(f"[bold]close rate[/bold] {money['close_rate']:.1f}% of conversations started")


def _money(amount, currency: str | None) -> str:
    if amount in (None, ""):
        return "—"
    symbol = currency or ""
    text = f"{float(amount):,.0f}"
    if symbol in ("$", "£", "€"):
        return f"{symbol}{text}"
    return f"{text} {symbol}".strip()


def cmd_stats(args, cfg) -> None:
    with db.session(cfg.db_path) as conn:
        counts = db.counts(conn)
        types = conn.execute(
            "SELECT lead_type, COUNT(*) n FROM leads WHERE qualified = 1 GROUP BY lead_type"
        ).fetchall()
        channels = conn.execute(
            "SELECT channel, status, COUNT(*) n FROM messages GROUP BY channel, status"
        ).fetchall()
        top_skips = conn.execute(
            """SELECT skip_reason, COUNT(*) n FROM leads WHERE status='SKIPPED'
               GROUP BY skip_reason ORDER BY n DESC LIMIT 5"""
        ).fetchall()
        grades = conn.execute(
            "SELECT lead_grade, COUNT(*) n FROM leads WHERE qualified = 1 "
            "AND lead_grade IS NOT NULL GROUP BY lead_grade ORDER BY lead_grade"
        ).fetchall()
        money = db.revenue(conn)

    table = Table(title="leads by status", show_header=True, header_style="bold")
    table.add_column("status")
    table.add_column("count", justify="right")
    for status in ("NEW", "AUDITED", "DRAFTED", "SENT", "REPLIED", "SKIPPED", "FAILED"):
        if counts.get(status):
            table.add_row(status, str(counts[status]))
    table.add_row("[bold]total[/bold]", f"[bold]{sum(counts.values())}[/bold]")
    console.print(table)

    if grades:
        console.print("\n[bold]lead quality[/bold]")
        for row in grades:
            console.print(f"  grade {row['lead_grade']}: {row['n']}")

    if types:
        console.print("\n[bold]qualified leads[/bold]")
        for row in types:
            console.print(f"  {row['lead_type'] or 'unknown'}: {row['n']}")

    if channels:
        console.print("\n[bold]messages[/bold]")
        for row in channels:
            console.print(f"  {row['channel']} {row['status']}: {row['n']}")

    sent = counts.get("SENT", 0)
    replied = counts.get("REPLIED", 0)
    if sent or replied:
        rate = replied / (sent + replied) * 100 if (sent + replied) else 0
        console.print(f"\n[bold]reply rate[/bold] {replied}/{sent + replied} ({rate:.1f}%)")

    if top_skips:
        console.print("\n[bold]top skip reasons[/bold]")
        for row in top_skips:
            console.print(f"  {row['n']:>4}  {row['skip_reason']}")

    # Last, and on its own, because it is the only number that is money.
    console.print()
    _print_funnel(money)


def cmd_list(args, cfg) -> None:
    where, params = _lead_filters(args)

    with db.session(cfg.db_path) as conn:
        if args.by == "date":
            rows = db.leads_by_day(conn, args.limit or 30, where, params)
            _print_groups(rows, "day", "found on")
            return
        if args.by == "category":
            rows = db.leads_by_category(conn, args.limit or 30, where, params)
            _print_groups(rows, "category", "category")
            return

        sql = f"SELECT l.*, {db.LOCAL_DAY_SQL} AS day, {db.CATEGORY_SQL} AS group_category FROM leads l"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # Newest batch first, best lead first inside it: the natural way to work
        # through a morning's results.
        sql += " ORDER BY day DESC, COALESCE(l.lead_score, -1) DESC, l.id LIMIT ?"
        rows = conn.execute(sql, [*params, args.limit or 50]).fetchall()

    if not rows:
        console.print("nothing matches those filters.")
        return

    # Grouped under a date heading rather than repeating the date on every row:
    # the blank line between batches is what makes a long list readable.
    current_day = None
    table = None
    for row in rows:
        if row["day"] != current_day:
            if table is not None:
                console.print(table)
                console.print()
            current_day = row["day"]
            console.rule(f"[bold]{_day_label(current_day)}[/bold]", align="left")
            table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
            for col in ("id", "name", "category", "grade", "score", "type", "status", "time"):
                table.add_column(col)
        table.add_row(
            str(row["id"]),
            (row["name"] or "")[:26],
            (row["group_category"] or "")[:18],
            row["lead_grade"] or "—",
            str(row["lead_score"]) if row["lead_score"] is not None else "—",
            row["lead_type"] or "—",
            row["status"],
            _clock(row["created_at"]),
        )
    if table is not None:
        console.print(table)


def _lead_filters(args) -> tuple[list[str], list]:
    where: list[str] = []
    params: list = []
    if getattr(args, "status", None):
        where.append("l.status = ?")
        params.append(args.status.upper())
    if getattr(args, "category", None):
        # Substring, case-insensitive: nobody types "Roofing contractor" exactly.
        where.append(f"lower({db.CATEGORY_SQL}) LIKE ?")
        params.append(f"%{args.category.lower()}%")
    if getattr(args, "on", None):
        where.append(f"{db.LOCAL_DAY_SQL} = ?")
        params.append(args.on)
    if getattr(args, "since", None):
        where.append(f"{db.LOCAL_DAY_SQL} >= ?")
        params.append(args.since)
    return where, params


def _print_groups(rows, key: str, heading: str) -> None:
    if not rows:
        console.print("no leads yet.")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column(heading)
    for col in ("found", "qualified", "contacted", "avg score"):
        table.add_column(col, justify="right")
    for row in rows:
        label = _day_label(row[key]) if key == "day" else (row[key] or "uncategorised")
        table.add_row(
            label[:34],
            str(row["found"]),
            str(row["qualified"] or 0),
            str(row["contacted"] or 0),
            f"{row['avg_score']:.0f}" if row["avg_score"] is not None else "—",
        )
    console.print(table)
    if key == "category":
        console.print(
            "[dim]Sorted by average lead score — the top rows are the trades "
            "worth searching more of.[/dim]"
        )


def _day_label(day: str | None) -> str:
    """A date, plus 'today'/'yesterday' where that is what someone means."""
    if not day:
        return "unknown date"
    from datetime import date, timedelta

    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        return day
    today = date.today()
    if parsed == today:
        return f"{day} (today)"
    if parsed == today - timedelta(days=1):
        return f"{day} (yesterday)"
    return f"{day} ({parsed.strftime('%a')})"


def _clock(iso: str | None) -> str:
    """Local HH:MM. The date is already the group heading."""
    if not iso:
        return "—"
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return iso[11:16]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%H:%M")


def cmd_export(args, cfg) -> None:
    # Resolved against the driving config so a test or second-campaign config
    # can never overwrite the export belonging to a different one.
    path = cfg.path.parent / "out" / "leads.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with db.session(cfg.db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT l.*,
                   {db.LOCAL_DAY_SQL} AS found_day, {db.CATEGORY_SQL} AS group_category,
                   a.audited_at,
                   a.score, a.issues_json, a.headline_issue,
                   a.opportunity, a.gaps_json, a.has_booking, a.has_quote_form,
                   a.has_services, a.has_contact, a.ecommerce,
                   p.stage AS pipeline_stage, p.quote_amount, p.won_amount,
                   p.currency, p.lost_reason,
                   (SELECT value FROM contacts c WHERE c.lead_id = l.id AND c.kind='email' LIMIT 1) AS email,
                   m.channel, m.subject, m.body, m.status AS message_status, m.sent_at
            FROM leads l
            LEFT JOIN audits a ON a.lead_id = l.id
            LEFT JOIN pipeline p ON p.lead_id = l.id
            LEFT JOIN messages m ON m.lead_id = l.id
            ORDER BY l.id
            """
        ).fetchall()

    if not rows:
        console.print("nothing to export.")
        return

    def defang(value):
        # A business "name" of =HYPERLINK(...) would execute when the CSV is
        # opened in Excel/Sheets. The leading apostrophe is the standard guard.
        if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t"):
            return "'" + value
        return value

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
        writer.writeheader()
        for row in rows:
            record = dict(row)
            if record.get("issues_json"):
                record["issues_json"] = "; ".join(describe_issues(record["issues_json"]))
            if record.get("gaps_json"):
                record["gaps_json"] = "; ".join(describe_gaps(record["gaps_json"]))
            writer.writerow({k: defang(v) for k, v in record.items()})
    console.print(f"[green]{len(rows)} rows[/green] → {path}")


def cmd_show(args, cfg) -> None:
    with db.session(cfg.db_path) as conn:
        lead = db.get_lead(conn, args.id)
        if not lead:
            console.print(f"no lead {args.id}")
            return
        audit_row = db.get_audit(conn, args.id)
        contacts = conn.execute(
            "SELECT kind, value, found_on FROM contacts WHERE lead_id = ?", (args.id,)
        ).fetchall()
        messages = conn.execute(
            "SELECT * FROM messages WHERE lead_id = ?", (args.id,)
        ).fetchall()
        stage_row = db.get_pipeline(conn, args.id)
        timeline = db.lead_timeline(conn, args.id)

    console.rule(f"[bold]{lead['name']}[/bold]")
    for key in ("category", "address", "phone", "website", "rating", "review_count",
                "status", "lead_type", "skip_reason", "source", "maps_url"):
        if lead[key] is not None:
            console.print(f"[dim]{key:<14}[/dim] {lead[key]}")
    console.print(f"[dim]{'found on':<14}[/dim] {_day_label(db.local_day(lead['created_at']))}"
                  f" at {_clock(lead['created_at'])}")

    verdict = qualify.explain(lead["score_json"])
    if verdict:
        console.print(
            f"\n[bold]lead score[/bold] {verdict['total']}/100 (grade {verdict['grade']})"
        )
        for name, value in verdict["components"].items():
            console.print(f"  [dim]{name.replace('_', ' '):<16}[/dim] {value}")
        for reason in verdict.get("reasons", []):
            console.print(f"  [green]+[/green] {reason}")
        for blocker in verdict.get("blockers", []):
            console.print(f"  [yellow]–[/yellow] {blocker}")

    # A business with no site has no site to be unreachable, so the technical
    # block is skipped entirely rather than printing zeros about nothing.
    if audit_row and lead["website"]:
        load = f"{audit_row['load_ms']}ms" if audit_row["reachable"] else "unreachable"
        console.print(f"\n[bold]technical defects[/bold] score {audit_row['score']}, "
                      f"{load}, https={bool(audit_row['https'])}")
        for issue in describe_issues(audit_row["issues_json"]) or ["none found"]:
            console.print(f"  · {issue}")

    if audit_row and audit_row["opportunity"] is not None:
        console.print(
            f"\n[bold]commercial opportunity[/bold] {audit_row['opportunity']}/100"
            + ("" if lead["website"] else " — they have no website at all")
        )
        for has, label in (
            (audit_row["has_booking"], "online booking"),
            (audit_row["has_quote_form"], "quote / enquiry route"),
            (audit_row["has_services"], "clear service information"),
            (audit_row["has_contact"], "obvious way to make contact"),
        ):
            mark = "[green]yes[/green]" if has else "[yellow]no[/yellow] "
            console.print(f"  {mark}  {label}")

    if timeline:
        console.print("\n[bold]history[/bold]")
        for event in timeline:
            console.print(
                f"  [dim]{event['day']} {_clock(event['at'])}[/dim]  {event['what']}"
            )

    if stage_row:
        console.print(f"\n[bold]sales pipeline[/bold] {STAGE_LABEL.get(stage_row['stage'], stage_row['stage'])}")
        for stage in db.ALL_STAGES:
            when = stage_row[db.STAGE_TIMESTAMP[stage]]
            if when:
                console.print(f"  [dim]{STAGE_LABEL[stage]:<22}[/dim] {when[:10]}")
        for label, value in (("quoted", stage_row["quote_amount"]),
                             ("won", stage_row["won_amount"])):
            if value is not None:
                console.print(f"  [dim]{label:<22}[/dim] {_money(value, stage_row['currency'])}")
        if stage_row["lost_reason"]:
            console.print(f"  [dim]{'lost because':<22}[/dim] {stage_row['lost_reason']}")


    if contacts:
        console.print("\n[bold]contacts[/bold]")
        for row in contacts:
            console.print(f"  {row['kind']}: {row['value']}")

    for message in messages:
        console.print(f"\n[bold]{message['channel']} message[/bold] ({message['status']})")
        if message["subject"]:
            console.print(f"[dim]subject:[/dim] {message['subject']}")
        console.print(message["body"])


def cmd_init(args, cfg) -> None:
    with db.session(cfg.db_path) as conn:
        conn.execute("SELECT 1")
    console.print(f"[green]database ready[/green] at {cfg.db_path}")

    missing = cfg.check_sender_ready()
    if missing:
        console.print(f"\n[yellow]Still to fill in, in {_edit_target(cfg)}:[/yellow]")
        for key in missing:
            console.print(f"  · {key}")
    else:
        console.print("sender identity looks filled in.")

    from .config import env

    provider = (cfg.get("llm.provider", "openai") or "openai").lower()
    draft_key = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    console.print("\n[bold]keys[/bold]")
    for name, why in (
        ("GOOGLE_PLACES_API_KEY", "lead discovery (optional — scraper is the fallback)"),
        (draft_key, f"message drafting (llm.provider: {provider})"),
        ("RESEND_API_KEY", "email sending (only if email is on)"),
    ):
        mark = "[green]set[/green]" if env(name) else "[red]missing[/red]"
        console.print(f"  {name}: {mark} [dim]({why})[/dim]")


# ============================================================ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prospector",
        description="Find local businesses with weak or missing websites, and message them.",
    )
    parser.add_argument("--config", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the database and check config")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("find", help="discover businesses from Google")
    p.add_argument("--niche")
    p.add_argument("--location")
    p.add_argument(
        "--source",
        choices=["auto", "places_api", "apify", "gosom", "scraper"],
        default=None,
        help="places_api = the supported default; apify = run an Apify Actor "
             "(bills your Apify account); gosom = the maintained Go scraper; "
             "scraper = the built-in Playwright one",
    )
    p.add_argument("--max", type=int)
    p.add_argument("--headful", action="store_true", help="show the scraper's browser")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("audit", help="check each lead's website and qualify it")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("draft", help="write a message for each qualified lead")
    p.add_argument("--limit", type=int)
    p.add_argument("--channel", choices=["auto", "email", "whatsapp"], default="auto")
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("preview", help="read drafts before anything goes out")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("send", help="send drafted emails (dry run without --yes)")
    p.add_argument("--limit", type=int)
    p.add_argument("--yes", action="store_true", help="actually send")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("wa", help="build the WhatsApp click-to-send queue page")
    p.add_argument("--limit", type=int)
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(func=cmd_wa)

    p = sub.add_parser("mark-sent", help="record WhatsApp messages you sent by hand")
    p.add_argument("ids", nargs="+", type=int)
    p.set_defaults(func=cmd_mark_sent)

    p = sub.add_parser("mark-replied", help="stop all future contact with these leads")
    p.add_argument("ids", nargs="+", type=int)
    p.set_defaults(func=cmd_mark_replied)

    p = sub.add_parser("suppress", help="never contact these emails/domains/phones")
    p.add_argument("values", nargs="+")
    p.add_argument("--kind", choices=["auto", "email", "domain", "phone"], default="auto")
    p.add_argument("--reason", default="manual")
    p.set_defaults(func=cmd_suppress)

    p = sub.add_parser("stats", help="stage counts, lead quality and revenue")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser(
        "pipeline",
        help="record a real sales step: reviewed, contacted, proposal, quoted, won, lost",
    )
    p.add_argument("stage", help="reviewed | contacted | proposal | quoted | won | lost")
    p.add_argument("ids", nargs="+", type=int)
    p.add_argument("--amount", type=float, help="quote or deal value — required for honest revenue")
    p.add_argument("--currency", help="e.g. $ or GBP; defaults to pipeline.currency")
    p.add_argument("--note")
    p.add_argument("--reason", help="why it was lost")
    p.set_defaults(func=cmd_pipeline)

    p = sub.add_parser("funnel", help="conversion by stage and revenue won")
    p.add_argument("--stage", help="only show deals currently at this stage")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_funnel)

    p = sub.add_parser("list", help="list leads, grouped by the day they were found")
    p.add_argument("--status")
    p.add_argument("--category", help="substring match on category or searched niche")
    p.add_argument("--on", metavar="YYYY-MM-DD", help="only leads found on this day")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="only leads found on or after this day")
    p.add_argument(
        "--by",
        choices=["lead", "date", "category"],
        default="lead",
        help="lead = every lead under a date heading (default); "
             "date / category = one summary row per group",
    )
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="everything known about one lead")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("export", help="dump everything to out/leads.csv")
    p.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    try:
        args.func(args, cfg)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    except KeyboardInterrupt:
        console.print("\ninterrupted")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
