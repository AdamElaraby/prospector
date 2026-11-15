"""Apify Actor source.

Apify runs scrapers ("Actors") on their infrastructure and hands back a
structured dataset over a normal REST API, so from this project's side it is
just another source: start a run, wait for it, read the dataset, normalise each
item into the same lead dict every other source produces.

    POST /v2/acts/{actorId}/runs            start a run
    GET  /v2/actor-runs/{runId}             poll its status
    GET  /v2/datasets/{datasetId}/items     read the results

Docs: https://docs.apify.com/api/v2

Google Places stays the supported default. This exists because Actors reach
things the Places API does not — and because an Actor you have already paid
for should be able to feed this pipeline rather than a spreadsheet.

Setup: put APIFY_API_TOKEN in .env, then point search.apify.actor_id at the
Actor you want. The default is the widely used Google Maps scraper Actor.

Costs real money on Apify's side. Nothing here retries a failed run, because a
retry loop against a metered API is how a £5 test becomes a £50 one.
"""

from __future__ import annotations

import time
from typing import Any, Iterator

import httpx

from ..config import env

BASE = "https://api.apify.com/v2"

# compass/crawler-google-places, in the path form the API expects (the tilde
# is Apify's separator for username~actor-name).
DEFAULT_ACTOR = "compass~crawler-google-places"

# Apify run states. Only SUCCEEDED yields data; the rest are all ways of being
# finished without any.
TERMINAL_OK = {"SUCCEEDED"}
TERMINAL_BAD = {"FAILED", "ABORTED", "TIMED-OUT", "TIMING-OUT"}


class ApifyError(RuntimeError):
    pass


class ApifySource:
    name = "apify"

    def __init__(
        self,
        actor_id: str | None = None,
        *,
        token: str | None = None,
        input_template: dict[str, Any] | None = None,
        query_field: str = "searchStringsArray",
        max_field: str = "maxCrawledPlacesPerSearch",
        timeout_seconds: int = 900,
        poll_seconds: int = 5,
        memory_mbytes: int | None = None,
        dataset_id: str | None = None,
    ) -> None:
        self.actor_id = (actor_id or DEFAULT_ACTOR).replace("/", "~")
        self.token = token or env("APIFY_API_TOKEN", required=True)
        self.input_template = dict(input_template or {})
        self.query_field = query_field
        self.max_field = max_field
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = max(2, poll_seconds)
        self.memory_mbytes = memory_mbytes
        # Set to read an existing dataset instead of paying for a new run —
        # useful for re-importing an Actor run you already did on Apify.
        self.dataset_id = dataset_id

    # ------------------------------------------------------------------ public

    def search(self, niche: str, location: str, max_results: int = 60) -> Iterator[dict]:
        with httpx.Client(timeout=60, headers={"Authorization": f"Bearer {self.token}"}) as client:
            if self.dataset_id:
                items = self._dataset_items(client, self.dataset_id, max_results)
            else:
                run = self._start(client, niche, location, max_results)
                run = self._wait(client, run["id"])
                items = self._dataset_items(client, run["defaultDatasetId"], max_results)

            for item in items:
                lead = self._normalize(item, niche, location)
                if lead:
                    yield lead

    # --------------------------------------------------------------- internals

    def _start(self, client: httpx.Client, niche: str, location: str, max_results: int) -> dict:
        payload: dict[str, Any] = dict(self.input_template)
        # The template wins only where it is not describing the search itself:
        # a hard-coded query in config would make every niche return the same
        # businesses while appearing to work.
        payload[self.query_field] = [f"{niche} in {location}"]
        if self.max_field:
            payload[self.max_field] = max_results
        payload.setdefault("language", "en")

        params: dict[str, Any] = {}
        if self.memory_mbytes:
            params["memory"] = self.memory_mbytes

        resp = client.post(f"{BASE}/acts/{self.actor_id}/runs", json=payload, params=params)
        if resp.status_code == 404:
            raise ApifyError(
                f"Apify has no Actor '{self.actor_id}'. Use the username~actor-name "
                "form, e.g. compass~crawler-google-places."
            )
        if resp.status_code in (401, 403):
            raise ApifyError("Apify rejected APIFY_API_TOKEN — check the key in .env.")
        if resp.status_code >= 400:
            raise ApifyError(f"Apify {resp.status_code} starting the run: {resp.text[:300]}")
        return resp.json()["data"]

    def _wait(self, client: httpx.Client, run_id: str) -> dict:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            resp = client.get(f"{BASE}/actor-runs/{run_id}")
            if resp.status_code >= 400:
                raise ApifyError(f"Apify {resp.status_code} polling the run: {resp.text[:300]}")
            data = resp.json()["data"]
            status = data.get("status")
            if status in TERMINAL_OK:
                return data
            if status in TERMINAL_BAD:
                raise ApifyError(
                    f"Apify run {run_id} ended as {status}. "
                    f"The run log is at https://console.apify.com/actors/runs/{run_id}"
                )
            if time.monotonic() > deadline:
                # The run keeps going on Apify and keeps billing; say so rather
                # than letting it look cancelled.
                raise ApifyError(
                    f"Apify run {run_id} is still {status} after {self.timeout_seconds}s. "
                    "It is still running on Apify — raise search.apify.timeout_seconds, "
                    "or abort it in the Apify console."
                )
            time.sleep(self.poll_seconds)

    def _dataset_items(
        self, client: httpx.Client, dataset_id: str, max_results: int
    ) -> list[dict]:
        if not dataset_id:
            raise ApifyError("the Apify run produced no dataset")
        items: list[dict] = []
        offset = 0
        while len(items) < max_results:
            resp = client.get(
                f"{BASE}/datasets/{dataset_id}/items",
                params={"offset": offset, "limit": min(500, max_results - len(items)),
                        "clean": "true", "format": "json"},
            )
            if resp.status_code >= 400:
                raise ApifyError(f"Apify {resp.status_code} reading the dataset: {resp.text[:300]}")
            page = resp.json()
            if not isinstance(page, list) or not page:
                break
            items.extend(page)
            offset += len(page)
        return items[:max_results]

    # --------------------------------------------------------------- mapping

    def _normalize(self, item: dict, niche: str, location: str) -> dict | None:
        """Map one dataset item onto the shared lead dict.

        Actors disagree about field names, so each value is read from a list of
        likely spellings. An item with no business name is dropped rather than
        stored as an unnamed row nobody can act on.
        """
        name = _first(item, "title", "name", "placeName", "businessName")
        if not name:
            return None

        website = _first(item, "website", "websiteUrl", "url", "domain")
        # Several Actors put the Google Maps link in `url`; that is not a site.
        if website and "google." in website.lower() and "/maps" in website.lower():
            website = None

        emails = _emails(item)

        return {
            "place_id": _first(item, "placeId", "place_id", "cid", "fid", "id"),
            "name": str(name).strip(),
            "category": _first(item, "categoryName", "category", "primaryCategory", "type"),
            "address": _first(item, "address", "formattedAddress", "fullAddress", "street"),
            "city": _first(item, "city", "locality") or location,
            "phone": _first(item, "phone", "phoneNumber", "phoneUnformatted",
                            "internationalPhoneNumber"),
            "website": (str(website).strip() or None) if website else None,
            "rating": _number(item, "totalScore", "rating", "stars", "averageRating"),
            "review_count": _int(item, "reviewsCount", "userRatingCount", "reviews",
                                 "numberOfReviews", "reviewCount"),
            "maps_url": _first(item, "url", "googleMapsUrl", "mapsUrl", "searchPageUrl"),
            "source": self.name,
            "search_niche": niche,
            "search_location": location,
            "business_status": _business_status(item),
            "emails": emails,
        }


# -------------------------------------------------------------------- helpers


def _first(item: dict, *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("text") or value.get("name")
        if value not in (None, "", [], {}):
            return value
    return None


def _number(item: dict, *keys: str) -> float | None:
    value = _first(item, *keys)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int(item: dict, *keys: str) -> int | None:
    value = _first(item, *keys)
    if isinstance(value, list):
        return len(value)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _emails(item: dict) -> list[str]:
    """Some Actors scrape contact emails during the crawl. Keep them."""
    value = item.get("emails") or item.get("email")
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip().lower() for v in value if isinstance(v, str) and "@" in v]


def _business_status(item: dict) -> str | None:
    """Normalise to the one value the pipeline acts on: CLOSED_PERMANENTLY."""
    raw = _first(item, "permanentlyClosed", "businessStatus", "status")
    if raw is True:
        return "CLOSED_PERMANENTLY"
    if isinstance(raw, str) and "permanently" in raw.lower().replace("_", " "):
        return "CLOSED_PERMANENTLY"
    if item.get("temporarilyClosed") is True:
        return "CLOSED_TEMPORARILY"
    return None
