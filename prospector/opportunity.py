"""Commercial opportunity: what the business cannot currently DO online.

Kept deliberately apart from audit.py, which answers a different question.

    audit.py        "is this site broken?"      -> technical defects
    opportunity.py  "can this site sell?"       -> commercial gaps

The distinction matters in the pitch and in the scoring. "Your certificate has
expired" is a complaint about a mistake; "there is no way for a customer to
book you at 11pm" is money they are not collecting. The second one sells, and
a site can be technically flawless and still have every gap below.

Four capabilities, because these are the ones a small local business actually
converts on:

    booking      can a customer commit to a time without phoning?
    quote        can a customer ask for a price without phoning?
    services     can a customer find out what is offered, and roughly for what?
    contact      is there an obvious, one-tap way to reach them at all?

Detection is conservative in one direction on purpose: a false "they have no
booking" produces a pitch about something they already have, which destroys the
message. So a weak signal counts as present.
"""

from __future__ import annotations

import json
from typing import Any

from bs4 import BeautifulSoup

# Points of commercial upside per missing capability, out of 100. Booking leads
# because it is the one that converts outside business hours, which is exactly
# when people look. Contact is weighted hard because missing it means the site
# cannot produce a customer at all.
GAP_WEIGHTS: dict[str, int] = {
    "no_booking": 30,
    "no_contact_route": 28,
    "no_quote_route": 22,
    "no_service_info": 20,
}

# What to say about each gap. Phrased as the customer's experience, never as a
# fault: these are capabilities to add, not errors to point out.
GAP_TEXT: dict[str, str] = {
    "no_booking": "a customer cannot book or reserve online — every appointment has to go through a phone call",
    "no_quote_route": "there is no way to request a price or a quote from the site",
    "no_service_info": "the site never says plainly what services are offered",
    "no_contact_route": "there is no obvious way to get in touch from the homepage",
}

# Third-party booking widgets. A link or an embedded script from any of these
# means booking exists even when no button on the page says so.
BOOKING_HOSTS = (
    "calendly.com", "acuityscheduling.com", "squareup.com/appointments",
    "book.squareup.com", "setmore.com", "simplybook.me", "booksy.com",
    "fresha.com", "treatwell.", "mindbodyonline.com", "vagaro.com",
    "schedulicity.com", "opentable.", "resy.com", "sevenrooms.com",
    "thefork.", "quandoo.", "tablein.com", "housecallpro.com",
    "jobber.com", "servicetitan.com", "getjobber.com", "appointy.com",
    "timify.com", "youcanbook.me", "picktime.com", "10to8.com",
    "bookingkoala.com", "launch27.com", "cal.com", "tidycal.com",
    "squarespace-scheduling.com", "appointlet.com", "zenoti.com",
)

BOOKING_WORDS = (
    "book now", "book online", "book a", "book an", "booking", "make a booking",
    "reserve a table", "reserve now", "reservation", "schedule an appointment",
    "schedule a", "schedule online", "book appointment", "make an appointment",
    "request an appointment", "appointment online", "order online",
    "buy tickets", "check availability",
)

QUOTE_WORDS = (
    "get a quote", "request a quote", "free quote", "request quote",
    "get an estimate", "free estimate", "request an estimate", "estimate request",
    "request a callback", "request pricing", "get pricing", "enquire", "inquire",
    "enquiry", "inquiry", "contact form", "send us a message", "get in touch form",
    "tell us about your project", "request a consultation", "book a consultation",
)

SERVICE_WORDS = (
    "our services", "services", "what we do", "what we offer", "treatments",
    "menu", "price list", "pricing", "prices", "packages", "rates",
    "service areas", "our work", "repairs", "specialties", "specialities",
)

# A services page that is only a nav label proves nothing, so a link alone is a
# weak signal and needs the page to also talk about what it sells.
SERVICE_PATH_HINTS = (
    "/services", "/service", "/menu", "/pricing", "/prices", "/price-list",
    "/treatments", "/packages", "/rates", "/what-we-do", "/our-work",
)

CONTACT_PATH_HINTS = ("/contact", "/contact-us", "/get-in-touch", "/enquiry", "/book")

ECOMMERCE_MARKERS = (
    "add to cart", "add to basket", "shopping cart", "checkout", "my account",
    "/cart", "/checkout", "shopify", "woocommerce", "bigcommerce", "magento",
    "snipcart", "ecwid",
)

# Form field names that mean "tell us what you need", as opposed to a newsletter
# signup, a login, or a site search — none of which is a way to ask for a price.
QUOTE_FIELD_HINTS = (
    "message", "enquiry", "inquiry", "project", "details", "comments",
    "description", "requirement", "service", "quote", "estimate", "how can we help",
)
NOT_A_QUOTE_FORM = ("search", "newsletter", "subscribe", "login", "sign in", "password")


def analyse(soup: BeautifulSoup, html: str, base_url: str) -> dict[str, Any]:
    """Look at one fetched homepage. Returns audit columns plus 'gaps'."""
    text = soup.get_text(" ", strip=True).lower()
    hrefs = [(a.get("href") or "").lower() for a in soup.find_all("a", href=True)]
    link_text = " ".join(a.get_text(" ", strip=True).lower() for a in soup.find_all("a"))
    buttons = " ".join(
        b.get_text(" ", strip=True).lower() for b in soup.find_all(["button", "summary"])
    )
    srcs = [
        (tag.get("src") or tag.get("data-src") or "").lower()
        for tag in soup.find_all(["script", "iframe"])
    ]
    clickable = f"{link_text} {buttons}"
    haystack = " ".join(hrefs + srcs)

    has_booking = _has_booking(clickable, haystack, text)
    has_quote = _has_quote_route(soup, clickable, hrefs)
    has_services = _has_service_info(soup, clickable, hrefs, text)
    has_contact = _has_contact_route(soup, hrefs, clickable, text)
    ecommerce = any(marker in f"{text} {haystack}" for marker in ECOMMERCE_MARKERS)

    return _result(
        has_booking=has_booking,
        has_quote=has_quote,
        has_services=has_services,
        has_contact=has_contact,
        ecommerce=ecommerce,
    )


def no_website() -> dict[str, Any]:
    """A business with no site has none of these capabilities, by definition.

    Stated explicitly rather than left as nulls, because the pitch to a
    no-website business is exactly this list, and the qualifier needs a real
    number from it rather than a missing one.
    """
    return _result(
        has_booking=False,
        has_quote=False,
        has_services=False,
        has_contact=False,
        ecommerce=False,
    )


def unknown() -> dict[str, Any]:
    """The site exists but we never saw it (bot-blocked, dead, timed out).

    Everything stays null. Scoring an unseen site as "no booking" would invent
    an opportunity, and the message built on it would be a false claim.
    """
    return {
        "has_booking": None,
        "has_quote_form": None,
        "has_services": None,
        "has_contact": None,
        "ecommerce": None,
        "gaps_json": None,
        "opportunity": None,
        "headline_gap": None,
        "gaps": [],
    }


def _result(
    *, has_booking: bool, has_quote: bool, has_services: bool, has_contact: bool,
    ecommerce: bool,
) -> dict[str, Any]:
    gaps: list[str] = []
    if not has_booking:
        gaps.append("no_booking")
    if not has_quote:
        gaps.append("no_quote_route")
    if not has_services:
        gaps.append("no_service_info")
    if not has_contact:
        gaps.append("no_contact_route")

    return {
        "has_booking": int(has_booking),
        "has_quote_form": int(has_quote),
        "has_services": int(has_services),
        "has_contact": int(has_contact),
        "ecommerce": int(ecommerce),
        "gaps_json": json.dumps(gaps),
        "opportunity": sum(GAP_WEIGHTS[g] for g in gaps),
        "headline_gap": GAP_TEXT[gaps[0]] if gaps else None,
        "gaps": gaps,
    }


# ------------------------------------------------------------------- detectors


def _has_booking(clickable: str, haystack: str, text: str) -> bool:
    if any(host in haystack for host in BOOKING_HOSTS):
        return True
    if any(word in clickable for word in BOOKING_WORDS):
        return True
    # A booking phrase in body copy is weaker than one on a button, but
    # "book online at any time" still means a booking route exists somewhere.
    return any(word in text for word in ("book online", "book now", "reserve online",
                                         "schedule an appointment", "book an appointment"))


def _has_quote_route(soup: BeautifulSoup, clickable: str, hrefs: list[str]) -> bool:
    if any(word in clickable for word in QUOTE_WORDS):
        return True
    if any(hint in href for href in hrefs for hint in ("/quote", "/estimate", "/enquir", "/inquir")):
        return True
    return _has_enquiry_form(soup)


def _has_enquiry_form(soup: BeautifulSoup) -> bool:
    """A form a customer can describe a job in — not a search or signup box."""
    for form in soup.find_all("form"):
        blob = " ".join(
            filter(
                None,
                [
                    (form.get("id") or ""),
                    (form.get("name") or ""),
                    " ".join(form.get("class") or []),
                    (form.get("action") or ""),
                    form.get_text(" ", strip=True)[:300],
                ],
            )
        ).lower()
        if any(bad in blob for bad in NOT_A_QUOTE_FORM):
            continue
        fields = " ".join(
            f"{(i.get('name') or '')} {(i.get('placeholder') or '')} {(i.get('id') or '')}"
            for i in form.find_all(["input", "textarea", "select"])
        ).lower()
        if form.find("textarea") or any(hint in f"{blob} {fields}" for hint in QUOTE_FIELD_HINTS):
            return True
    return False


def _has_service_info(
    soup: BeautifulSoup, clickable: str, hrefs: list[str], text: str
) -> bool:
    linked = any(hint in href for href in hrefs for hint in SERVICE_PATH_HINTS)
    named = any(word in clickable for word in SERVICE_WORDS)
    headings = " ".join(
        h.get_text(" ", strip=True).lower() for h in soup.find_all(["h1", "h2", "h3"])
    )
    described = any(word in headings for word in SERVICE_WORDS)
    # A nav label on its own is not information. Either the homepage itself
    # describes the work, or there is a dedicated page AND enough copy that the
    # page is plausibly real rather than an empty template.
    return described or ((linked or named) and len(text) > 600)


def _has_contact_route(
    soup: BeautifulSoup, hrefs: list[str], clickable: str, text: str
) -> bool:
    if soup.select_one('a[href^="tel:"]') or soup.select_one('a[href^="mailto:"]'):
        return True
    if any(hint in href for href in hrefs for hint in CONTACT_PATH_HINTS):
        return True
    if "contact" in clickable or "call us" in text or "email us" in text:
        return True
    return _has_enquiry_form(soup)


# -------------------------------------------------------------------- helpers


def describe_gaps(gaps_json: str | None) -> list[str]:
    try:
        gaps = json.loads(gaps_json or "[]")
    except json.JSONDecodeError:
        return []
    return [GAP_TEXT.get(g, g) for g in gaps]


def gap_labels(gaps_json: str | None) -> list[str]:
    """Short labels for a table cell, e.g. ['no booking', 'no quote form']."""
    short = {
        "no_booking": "no booking",
        "no_quote_route": "no quote form",
        "no_service_info": "no service info",
        "no_contact_route": "no contact route",
    }
    try:
        gaps = json.loads(gaps_json or "[]")
    except json.JSONDecodeError:
        return []
    return [short.get(g, g) for g in gaps]
