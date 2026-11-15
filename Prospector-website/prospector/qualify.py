"""Lead qualification: is this business worth your afternoon?

"No website" is not a qualification. A dead barber shop with two reviews and a
disconnected number also has no website, and messaging it costs the same as
messaging a busy one — but it will never reply, and if it does it cannot pay.
Counting those as leads is how a pipeline fills up with nothing.

So a lead is scored on four things, and has to clear a bar on three of them
independently. A high total built entirely out of one dimension does not pass:

    activity     (0-30)  Is this a going concern? Reviews, rating, a phone.
    problem      (0-30)  Is there a demonstrated problem worth fixing — a real
                         technical defect, or a missing commercial capability?
    ability      (0-25)  Can they plausibly pay? Volume and trade as proxies.
    deliverable  (0-15)  Can one person realistically build what they need?

Everything here reads data already collected. No extra requests, no model call.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

GRADES = (("A", 75), ("B", 60), ("C", 45), ("D", 0))

# Trades where a small site is bought by the owner, in one conversation, out of
# this month's takings. These are the ones that actually convert.
DEFAULT_HIGH_VALUE = (
    "plumb", "electric", "hvac", "heating", "roof", "landscap", "garden",
    "pest", "lock", "remov", "mover", "moving", "clean", "detail", "paving",
    "builder", "contractor", "renovat", "kitchen", "bathroom", "flooring",
    "glazier", "window", "fencing", "driveway", "tree surgeon", "scaffold",
    "mechanic", "garage", "auto repair", "tyre", "tire", "body shop",
    "dentist", "orthodont", "physio", "chiropract", "veterinar", "optician",
    "med spa", "aesthetic", "laser", "cosmetic", "wedding", "photograph",
    "caterer", "catering", "event", "driving school", "tutor", "law",
    "account", "surveyor", "estate agent", "letting", "architect",
)

# Trades that are real businesses but almost never buy a website: margins are
# thin, the owner is on the floor all day, and walk-in trade is the whole model.
DEFAULT_LOW_VALUE = (
    "laundromat", "launderette", "vending", "atm", "newsagent", "convenience",
    "corner shop", "kiosk", "market stall", "charity shop", "thrift",
    "food truck", "ice cream van", "car park", "parking", "storage unit",
)

# Things one person building brochure sites cannot realistically deliver, so a
# "lead" here is a meeting that ends in an apology.
DEFAULT_UNDELIVERABLE = (
    "hospital", "university", "college", "airport", "city of", "county of",
    "government", "department of", "police", "fire department", "embassy",
    "bank", "credit union", "insurance comp", "hotel chain", "supermarket",
    "shopping mall", "stadium", "casino", "marketplace", "franchise opportunit",
)


def score_lead(
    lead: sqlite3.Row,
    audit: sqlite3.Row | None,
    cfg,
) -> dict[str, Any]:
    """Score one lead. Returns components, total, grade, reasons and a verdict.

    `audit` may be None (nothing checked yet) or may carry null commercial
    columns (the site blocked us). Both cases score the problem dimension from
    what is actually known, never from an assumption about what was not seen.
    """
    high = _patterns(cfg, "qualify.high_value_categories", DEFAULT_HIGH_VALUE)
    low = _patterns(cfg, "qualify.low_value_categories", DEFAULT_LOW_VALUE)
    undeliverable = _patterns(cfg, "qualify.undeliverable_patterns", DEFAULT_UNDELIVERABLE)

    reasons: list[str] = []
    blockers: list[str] = []

    activity = _activity(lead, reasons)
    problem = _problem(lead, audit, reasons, blockers)
    ability = _ability(lead, high, low, reasons)
    deliverable = _deliverable(lead, audit, undeliverable, reasons, blockers)

    total = activity + problem + ability + deliverable
    grade = next(g for g, floor in GRADES if total >= floor)

    min_total = int(cfg.get("qualify.min_lead_score", 55))
    min_activity = int(cfg.get("qualify.min_activity_score", 12))
    min_problem = int(cfg.get("qualify.min_problem_score", 10))

    # Each gate is checked on its own so the reason given is the true one. A
    # single "score too low" message hides whether the business is dead, fine
    # as it is, or simply out of reach.
    if activity < min_activity:
        blockers.append(f"not enough sign of an active business (activity {activity}/{min_activity})")
    if problem < min_problem:
        blockers.append(f"no problem worth pitching (problem {problem}/{min_problem})")
    if total < min_total:
        blockers.append(f"overall score {total} is under the {min_total} bar")

    return {
        "total": total,
        "grade": grade,
        "components": {
            "activity": activity,
            "problem": problem,
            "ability_to_pay": ability,
            "deliverable": deliverable,
        },
        "reasons": reasons,
        "blockers": blockers,
        "qualified": not blockers,
        "skip_reason": blockers[0] if blockers else None,
    }


def to_json(result: dict[str, Any]) -> str:
    return json.dumps(
        {
            "total": result["total"],
            "grade": result["grade"],
            "components": result["components"],
            "reasons": result["reasons"],
            "blockers": result["blockers"],
        }
    )


def explain(score_json: str | None) -> dict[str, Any] | None:
    try:
        return json.loads(score_json) if score_json else None
    except json.JSONDecodeError:
        return None


# ------------------------------------------------------------------ dimensions


def _activity(lead: sqlite3.Row, reasons: list[str]) -> int:
    """Is anyone home? Reviews are the only trading signal Google gives us.

    Nothing here disqualifies on its own — a quiet listing is a weak signal,
    not proof of a dead business. The activity gate in score_lead decides,
    once every signal has been counted.
    """
    score = 0
    reviews = lead["review_count"] or 0
    rating = lead["rating"]

    for floor, points in ((150, 14), (60, 12), (25, 10), (10, 7), (3, 4), (1, 2)):
        if reviews >= floor:
            score += points
            break
    if reviews >= 25:
        reasons.append(f"{reviews} Google reviews — clearly trading")
    elif reviews == 0:
        reasons.append("no Google reviews at all — may not be trading")

    if rating is None:
        score += 1
    elif rating >= 4.5:
        score += 8
        reasons.append(f"{rating}★ — customers like them, so there is something to sell")
    elif rating >= 4.0:
        score += 6
    elif rating >= 3.5:
        score += 3
    elif rating < 3.0 and reviews >= 10:
        # A genuinely disliked business is a hard sell and a worse client.
        reasons.append(f"{rating}★ over {reviews} reviews — reputation problem, not a website problem")

    if lead["phone"]:
        score += 6
    else:
        reasons.append("no phone number on the listing — often a sign it is stale")
    if lead["maps_url"]:
        score += 2

    return min(score, 30)


def _problem(
    lead: sqlite3.Row, audit: sqlite3.Row | None, reasons: list[str], blockers: list[str]
) -> int:
    """A demonstrated problem — technical, commercial, or simply no site.

    Capped per source so neither side can carry the dimension alone: ten small
    technical nits are not the same thing as a business that cannot be booked.
    """
    if lead["lead_type"] == "NO_SITE" or not lead["website"]:
        reasons.append("no website at all — every online capability is missing")
        return 26

    if audit is None:
        blockers.append("not audited yet, so no problem has been demonstrated")
        return 0

    if audit["blocked"]:
        blockers.append("their site blocked the audit, so nothing about it is proven")
        return 0

    technical = min(int(audit["score"] or 0) * 2, 16)
    if technical:
        reasons.append(f"audit found real defects worth {audit['score']} points")

    commercial = 0
    if audit["opportunity"] is not None:
        # 100 points of commercial gap maps to 14 here: a site missing every
        # capability is a strong lead, but never as strong as having no site.
        commercial = round(int(audit["opportunity"]) * 14 / 100)
        if audit["headline_gap"]:
            reasons.append(audit["headline_gap"])

    total = technical + commercial
    if total == 0:
        reasons.append("their site works and does its job")
    return min(total, 30)


def _ability(lead: sqlite3.Row, high: tuple[str, ...], low: tuple[str, ...],
             reasons: list[str]) -> int:
    """Can they pay? Nothing here is certain — these are the honest proxies."""
    score = 0
    reviews = lead["review_count"] or 0

    # Review volume is the closest thing to a revenue signal that is free.
    for floor, points in ((200, 12), (75, 10), (30, 8), (10, 5), (3, 3)):
        if reviews >= floor:
            score += points
            break

    blob = f"{lead['category'] or ''} {lead['search_niche'] or ''} {lead['name'] or ''}".lower()
    if any(p in blob for p in high):
        score += 9
        reasons.append("a trade where a website pays for itself in one job")
    elif any(p in blob for p in low):
        score += 1
        reasons.append("a trade that rarely buys a website — thin margins, walk-in trade")
    else:
        score += 5

    if (lead["rating"] or 0) >= 4.0 and reviews >= 20:
        score += 4  # busy and liked: the ones with money this month

    return min(score, 25)


def _deliverable(
    lead: sqlite3.Row, audit: sqlite3.Row | None, undeliverable: tuple[str, ...],
    reasons: list[str], blockers: list[str],
) -> int:
    """Could you actually build this, alone, for the kind of money being asked?"""
    blob = f"{lead['category'] or ''} {lead['name'] or ''}".lower()
    if any(p in blob for p in undeliverable):
        blockers.append("too big or too regulated to be a realistic one-person job")
        return 0

    score = 10
    if audit is not None and audit["ecommerce"]:
        # A live store means stock, payments and a migration, not a brochure
        # site. Still possible, but no longer a weekend and a flat fee.
        score -= 6
        reasons.append("runs an online store — a rebuild is a much bigger job than a brochure site")
    elif lead["lead_type"] == "NO_SITE" or not lead["website"]:
        score += 5
        reasons.append("nothing to migrate — a clean build")
    else:
        score += 3

    return max(0, min(score, 15))


# --------------------------------------------------------------------- helpers


def _patterns(cfg, key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    value = cfg.get(key)
    if not value:
        return fallback
    return tuple(str(v).lower() for v in value)
