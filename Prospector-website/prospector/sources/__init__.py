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

    Apify is never chosen by 'auto'. Every Apify run bills the user's account,
    so it has to be asked for by name.
    """
    choice = (override or cfg.get("search.source", "auto")).lower()

    if choice == "auto":
        choice = _auto_choice(cfg)

    if choice in ("places_api", "api", "places"):
        from .places_api import PlacesAPISource

        return PlacesAPISource()

    if choice == "apify":
        from .apify import ApifySource

        return ApifySource(
            actor_id=cfg.get("search.apify.actor_id") or None,
            input_template=cfg.get("search.apify.input") or {},
            query_field=str(cfg.get("search.apify.query_field", "searchStringsArray")),
            max_field=str(cfg.get("search.apify.max_field", "maxCrawledPlacesPerSearch")),
            timeout_seconds=int(cfg.get("search.apify.timeout_seconds", 900)),
            poll_seconds=int(cfg.get("search.apify.poll_seconds", 5)),
            memory_mbytes=cfg.get("search.apify.memory_mbytes") or None,
            dataset_id=cfg.get("search.apify.dataset_id") or None,
        )

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
        f"Unknown source '{choice}'. Use places_api, apify, gosom, scraper, or auto."
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
