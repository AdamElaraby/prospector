"""Google Maps scraper (Playwright fallback for when there is no API key).

This drives a real browser against google.com/maps, scrolls the results feed,
and reads each card. It is the fragile path by nature: Google's class names are
obfuscated and rotate, so if extraction suddenly returns empty fields, the
selectors in CARD_SELECTORS below are the thing to update.

Be considerate with it. Scrolling thousands of results from one IP gets you
rate-limited, and it is against Google's terms of service. The Places API is
the supported route; this exists so the pipeline still runs without a key.

Run once before first use:
    playwright install chromium
"""

from __future__ import annotations

import re
import time
from typing import Iterator
from urllib.parse import quote_plus

CARD_SELECTORS = {
    "feed": 'div[role="feed"]',
    "card_link": "a.hfpxzc",             # anchor wrapping each result; aria-label = name
    "rating": "span.MW4etd",
    "reviews": "span.UY7F9",
    "aria_rating": 'span[role="img"][aria-label]',
    "detail_rating_block": "div.F7nice",
    "info_rows": "div.W4Efsd",
    "website_btn": 'a[data-value="Website"]',
    "end_marker": "span.HlvSq",          # "You've reached the end of the list."
    "detail_title": "h1.DUwDvf",
    "detail_phone": 'button[data-item-id^="phone:tel:"]',
    "detail_website": 'a[data-item-id="authority"]',
    "detail_category": "button.DkEaL",
    "detail_address": 'button[data-item-id="address"]',
}

PLACE_ID_RE = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)")


class MapsScraperSource:
    name = "maps_scraper"

    def __init__(self, headless: bool = True, slow_mo: int = 0) -> None:
        self.headless = headless
        self.slow_mo = slow_mo

    def search(self, niche: str, location: str, max_results: int = 60) -> Iterator[dict]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Playwright is not installed. Run:\n"
                "  pip install playwright && playwright install chromium"
            ) from exc

        query = f"{niche} in {location}"
        url = f"https://www.google.com/maps/search/{quote_plus(query)}/?hl=en"

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless, slow_mo=self.slow_mo)
            context = browser.new_context(
                locale="en-US",
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            self._dismiss_consent(page)

            try:
                page.wait_for_selector(CARD_SELECTORS["feed"], timeout=20_000)
            except Exception:
                browser.close()
                raise RuntimeError(
                    "Results feed never appeared. Google may be showing a consent "
                    "wall or a CAPTCHA. Re-run with --headful to look at it."
                )

            self._scroll_feed(page, max_results)
            cards = self._read_cards(page, niche, location, max_results)

            for card in cards:
                if card.get("needs_detail"):
                    self._fill_detail(page, card)
                card.pop("needs_detail", None)
                yield card

            browser.close()

    # ----------------------------------------------------------------- helpers

    def _dismiss_consent(self, page) -> None:
        """Google's EU/consent interstitial blocks the feed when it appears."""
        for label in ("Reject all", "Alle ablehnen", "Tout refuser", "Accept all"):
            try:
                btn = page.get_by_role("button", name=label)
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
                    page.wait_for_timeout(1500)
                    return
            except Exception:
                continue

    def _scroll_feed(self, page, target: int) -> None:
        feed = page.locator(CARD_SELECTORS["feed"])
        last_count = -1
        stagnant = 0

        while stagnant < 3:
            count = page.locator(CARD_SELECTORS["card_link"]).count()
            if count >= target:
                return
            if page.locator(CARD_SELECTORS["end_marker"]).count():
                return
            if count == last_count:
                stagnant += 1
            else:
                stagnant = 0
            last_count = count

            try:
                feed.evaluate("el => el.scrollBy(0, el.scrollHeight)")
            except Exception:
                return
            page.wait_for_timeout(1800)

    def _read_cards(self, page, niche: str, location: str, limit: int) -> list[dict]:
        """Pull what the results feed exposes without opening each place."""
        raw = page.evaluate(
            """
            (sel) => {
              const out = [];
              const links = document.querySelectorAll(sel.card_link);
              for (const a of links) {
                const card = a.closest('div[jsaction]') || a.parentElement;
                const text = (node, q) => {
                  const el = node.querySelector(q);
                  return el ? el.textContent.trim() : null;
                };
                const rows = card
                  ? Array.from(card.querySelectorAll(sel.info_rows)).map(e => e.textContent.trim())
                  : [];
                out.push({
                  name: a.getAttribute('aria-label'),
                  href: a.href,
                  rating: card ? text(card, sel.rating) : null,
                  reviews: card ? text(card, sel.reviews) : null,
                  rows: rows,
                  aria: card && card.querySelector(sel.aria_rating)
                    ? card.querySelector(sel.aria_rating).getAttribute('aria-label') : null,
                  has_website: card ? !!card.querySelector(sel.website_btn) : false,
                  website: card && card.querySelector(sel.website_btn)
                    ? card.querySelector(sel.website_btn).href : null,
                });
              }
              return out;
            }
            """,
            CARD_SELECTORS,
        )

        results: list[dict] = []
        for item in raw[:limit]:
            name = _clean(item.get("name"))
            if not name:
                continue
            href = item.get("href") or ""
            match = PLACE_ID_RE.search(href)
            category, address = self._split_info_rows(item.get("rows") or [])
            reviews = _to_int(item.get("reviews")) or _reviews_from_aria(item.get("aria"))

            results.append(
                {
                    "place_id": f"gmaps:{match.group(1)}" if match else None,
                    "name": name,
                    "category": category,
                    "address": address,
                    "city": location,
                    "phone": self._find_phone(item.get("rows") or []),
                    "website": item.get("website"),
                    "rating": _to_float(item.get("rating")),
                    "review_count": reviews,
                    "maps_url": href,
                    "source": self.name,
                    "search_niche": niche,
                    "search_location": location,
                    # Feed cards rarely carry a phone number, and that is the
                    # WhatsApp channel's only input, so open the detail panel.
                    "needs_detail": True,
                }
            )
        return results

    def _fill_detail(self, page, card: dict) -> None:
        """Open one place's detail panel to read phone / website / address."""
        if not card.get("maps_url"):
            return
        try:
            page.goto(card["maps_url"], wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_selector(CARD_SELECTORS["detail_title"], timeout=12_000)
            page.wait_for_timeout(600)

            phone_btn = page.locator(CARD_SELECTORS["detail_phone"]).first
            if phone_btn.count():
                item_id = phone_btn.get_attribute("data-item-id") or ""
                card["phone"] = item_id.replace("phone:tel:", "") or card.get("phone")

            site = page.locator(CARD_SELECTORS["detail_website"]).first
            if site.count():
                card["website"] = site.get_attribute("href") or card.get("website")

            if not card.get("address"):
                addr = page.locator(CARD_SELECTORS["detail_address"]).first
                if addr.count():
                    label = addr.get_attribute("aria-label") or ""
                    card["address"] = _clean(label.replace("Address:", "")) or None

            cat = page.locator(CARD_SELECTORS["detail_category"]).first
            if cat.count():
                detail_category = _clean(cat.text_content())
                # The feed's category is unreliable, so the detail page wins.
                if detail_category:
                    card["category"] = detail_category

            if card.get("review_count") is None:
                block = page.locator(CARD_SELECTORS["detail_rating_block"]).first
                if block.count():
                    card["review_count"] = _reviews_from_aria(block.text_content())
        except Exception:
            # A single unreadable place is not worth aborting the run over.
            pass
        finally:
            time.sleep(0.4)  # be polite between detail loads

    @staticmethod
    def _split_info_rows(rows: list[str]) -> tuple[str | None, str | None]:
        """Feed cards pack 'Category · Address' into dot-joined rows, but the
        first such row is usually the rating, and Google pads the rest with
        private-use-area icon glyphs. Both have to be filtered out."""
        category = address = None
        for row in rows:
            parts = [_clean(p) for p in row.split("·")]
            parts = [p for p in parts if p and not _is_rating(p) and not _looks_like_hours(p)]
            if not parts:
                continue
            if category is None:
                category = parts[0]
            if address is None and len(parts) >= 2:
                address = parts[1]
        return category, address

    @staticmethod
    def _find_phone(rows: list[str]) -> str | None:
        for row in rows:
            m = re.search(r"(\+?\d[\d\s().-]{7,}\d)", row)
            if m:
                return m.group(1).strip()
        return None


# Google pads result cards with Material icon glyphs from the Unicode
# private use area. They read as text but are not.
_GLYPH_RE = re.compile(r"[\ue000-\uf8ff]")


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    return _GLYPH_RE.sub("", text).strip(" ·⋅\u00a0\t\n") or None


def _is_rating(text: str) -> bool:
    """'4.5' and '4.5 (1,234)' are ratings, not categories."""
    return bool(re.fullmatch(r"\d[.,]\d(\s*\([\d,.\s]+\))?", text.strip()))


def _reviews_from_aria(text: str | None) -> int | None:
    """Pull the review count out of '4.5 stars 1,234 Reviews' or '4.5(1,234)'."""
    if not text:
        return None
    match = re.search(r"\(([\d.,\s]+)\)", text) or re.search(
        r"([\d.,\s]+)\s*(?:reviews?|avaliações|reseñas|avis)", text, re.I
    )
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def _looks_like_hours(text: str) -> bool:
    lowered = text.lower()
    return any(k in lowered for k in ("open", "clos", "24 hours", "am", "pm"))


def _to_float(value) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int | None:
    if value is None:
        return None
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else None
