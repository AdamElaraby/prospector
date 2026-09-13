"""Lead sources. Both return the same dict shape, so the rest of the pipeline
neither knows nor cares which one produced a lead.

Lead dict:
    place_id, name, category, address, city, phone, website,
    rating, review_count, maps_url, source
"""

from __future__ import annotations

from typing import Iterable, Protocol

from ..config import Config, env


class Source(Protocol):
    name: str

    def search(self, niche: str, location: str, max_results: int) -> Iterable[dict]:
        ...


def get_source(cfg: Config, override: str | None = None):
    """Resolve which source to use.

    'auto' prefers the structured API, then the maintained Go scraper if its
    binary is built, and falls back to the built-in Playwright scraper, which
    always works but is the most brittle of the three.
    """
    choice = (override or cfg.get("search.source", "auto")).lower()

    if choice == "auto":
        choice = _auto_choice(cfg)

    if choice in ("places_api", "api", "places"):
        from .places_api import PlacesAPISource

        return PlacesAPISource()

    if choice in ("gosom", "gms", "google-maps-scraper"):
        from .gosom_scraper import GosomSource

        return GosomSource(
            binary=cfg.get("search.gosom.binary"),
            depth=int(cfg.get("search.gosom.depth", 0)),
            concurrency=int(cfg.get("search.gosom.concurrency", 4)),
            lang=str(cfg.get("search.gosom.lang", "en")),
            extract_email=bool(cfg.get("search.gosom.extract_email", False)),
            timeout_seconds=int(cfg.get("search.gosom.timeout_seconds", 900)),
        )

    if choice in ("scraper", "maps", "maps_scraper", "playwright"):
        from .maps_scraper import MapsScraperSource

        return MapsScraperSource()

    raise ValueError(
        f"Unknown source '{choice}'. Use places_api, gosom, scraper, or auto."
    )


def _auto_choice(cfg: Config) -> str:
    if env("GOOGLE_PLACES_API_KEY"):
        return "places_api"
    try:
        from .gosom_scraper import find_binary

        find_binary(cfg.get("search.gosom.binary"))
        return "gosom"
    except Exception:
        return "scraper"
