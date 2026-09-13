"""Google Places API (New) text search.

Structured, stable, and it returns the field that matters most for this
campaign: websiteUri. A business with no websiteUri is a lead with no website.

Setup: Google Cloud console -> enable "Places API (New)" -> create an API key
-> put it in .env as GOOGLE_PLACES_API_KEY. Text Search is billed per request
(20 results per request); the standing free credit covers a lot of prospecting.
"""

from __future__ import annotations

import time
from typing import Iterator

import httpx

from ..config import env

ENDPOINT = "https://places.googleapis.com/v1/places:searchText"

FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.websiteUri",
        "places.rating",
        "places.userRatingCount",
        "places.primaryTypeDisplayName",
        "places.googleMapsUri",
        "places.businessStatus",
        "nextPageToken",
    ]
)


class PlacesAPISource:
    name = "places_api"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or env("GOOGLE_PLACES_API_KEY", required=True)

    def search(self, niche: str, location: str, max_results: int = 60) -> Iterator[dict]:
        query = f"{niche} in {location}"
        page_token: str | None = None
        seen = 0

        with httpx.Client(timeout=30) as client:
            while seen < max_results:
                payload: dict = {
                    "textQuery": query,
                    "pageSize": min(20, max_results - seen),
                }
                if page_token:
                    payload["pageToken"] = page_token

                resp = client.post(
                    ENDPOINT,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Goog-Api-Key": self.api_key,
                        "X-Goog-FieldMask": FIELD_MASK,
                    },
                )
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"Places API {resp.status_code}: {resp.text[:400]}"
                    )
                data = resp.json()

                places = data.get("places", []) or []
                if not places:
                    return
                for place in places:
                    seen += 1
                    yield self._normalize(place, niche, location)
                    if seen >= max_results:
                        return

                page_token = data.get("nextPageToken")
                if not page_token:
                    return
                # The next page token needs a moment before it is valid.
                time.sleep(2)

    def _normalize(self, place: dict, niche: str, location: str) -> dict:
        return {
            "place_id": place.get("id"),
            "name": (place.get("displayName") or {}).get("text", "").strip(),
            "category": place.get("primaryTypeDisplayName", {}).get("text")
            if isinstance(place.get("primaryTypeDisplayName"), dict)
            else place.get("primaryTypeDisplayName"),
            "address": place.get("formattedAddress"),
            "city": location,
            "phone": place.get("internationalPhoneNumber")
            or place.get("nationalPhoneNumber"),
            "website": (place.get("websiteUri") or "").strip() or None,
            "rating": place.get("rating"),
            "review_count": place.get("userRatingCount"),
            "maps_url": place.get("googleMapsUri"),
            "source": self.name,
            "search_niche": niche,
            "search_location": location,
            "business_status": place.get("businessStatus"),
        }
