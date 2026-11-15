"""gosom/google-maps-scraper (MIT) as a lead source.

Our own Playwright scraper reads Google's obfuscated DOM, which rots whenever
Google reshuffles class names. This tool is a maintained Go project that does
the same job and is kept working by people who watch for exactly that. When the
binary is available it is the better scraper; ours stays as the no-extra-deps
fallback.

It also returns the real Google place_id (ours can only derive a hash from the
result URL) and, with -email, addresses harvested from each business's site.

Install once:
    make install-gosom
or by hand:
    git clone https://github.com/gosom/google-maps-scraper /tmp/gms
    cd /tmp/gms && go build -o google-maps-scraper
    mv google-maps-scraper <this repo>/bin/

Point `search.gosom.binary` in config.local.yaml at it, or leave the default and put
it in ./bin/.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

from ..config import ROOT

DEFAULT_BINARY_PATHS = (
    ROOT / "bin" / "google-maps-scraper",
    ROOT / "google-maps-scraper",
)


class GosomNotFound(RuntimeError):
    pass


def find_binary(configured: str | None = None) -> Path:
    """Locate the scraper binary, or explain how to get one."""
    if configured:
        # An explicit config value that points nowhere is a misconfiguration to
        # report, not a hint to quietly use some other binary instead.
        explicit = Path(configured).expanduser()
        if explicit.is_file() and os.access(explicit, os.X_OK):
            return explicit
        raise GosomNotFound(
            f"search.gosom.binary points at {explicit}, which does not exist or "
            "is not executable. Fix the path or leave it blank for auto-detection."
        )

    candidates: list[Path] = []
    if os.environ.get("GOSOM_BINARY"):
        candidates.append(Path(os.environ["GOSOM_BINARY"]).expanduser())
    candidates.extend(DEFAULT_BINARY_PATHS)

    on_path = shutil.which("google-maps-scraper")
    if on_path:
        candidates.append(Path(on_path))

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate

    raise GosomNotFound(
        "google-maps-scraper binary not found. Build it with:\n"
        "    make install-gosom\n"
        "or set search.gosom.binary in config.local.yaml to an existing build.\n"
        "Meanwhile, --source scraper uses the built-in Playwright scraper."
    )


class GosomSource:
    name = "gosom"

    def __init__(
        self,
        binary: str | None = None,
        depth: int = 0,
        concurrency: int = 4,
        lang: str = "en",
        extract_email: bool = False,
        inactivity_timeout: str = "90s",
        timeout_seconds: int = 900,
    ) -> None:
        self.binary = find_binary(binary)
        self.depth = depth
        self.concurrency = concurrency
        self.lang = lang
        self.extract_email = extract_email
        self.inactivity_timeout = inactivity_timeout
        self.timeout_seconds = timeout_seconds

    def search(self, niche: str, location: str, max_results: int = 60) -> Iterator[dict]:
        query = f"{niche} in {location}"

        with tempfile.TemporaryDirectory(prefix="prospector-gosom-") as tmp:
            tmp_path = Path(tmp)
            query_file = tmp_path / "queries.txt"
            result_file = tmp_path / "results.json"
            query_file.write_text(query + "\n", encoding="utf-8")

            cmd = [
                str(self.binary),
                "-input", str(query_file),
                "-results", str(result_file),
                "-json",
                "-depth", str(self._depth_for(max_results)),
                "-c", str(self.concurrency),
                "-lang", self.lang,
                "-exit-on-inactivity", self.inactivity_timeout,
            ]
            if self.extract_email:
                cmd.append("-email")

            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    cwd=tmp_path,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(
                    f"google-maps-scraper exceeded {self.timeout_seconds}s. "
                    "Lower search.max_results_per_query or raise search.gosom.timeout_seconds."
                )

            # It exits non-zero on inactivity shutdown even after writing good
            # results, so trust the output file over the exit code — but a
            # non-zero exit with NO parseable results is a real failure, and
            # reporting it as "found 0" would hide the crash entirely.
            entries = list(_read_entries(result_file))
            if not entries and proc.returncode != 0:
                raise RuntimeError(
                    f"google-maps-scraper failed ({proc.returncode}): "
                    f"{(proc.stderr or proc.stdout or '')[-500:]}"
                )

            count = 0
            for entry in entries:
                if count >= max_results:
                    return
                normalized = self._normalize(entry, niche, location)
                if not normalized:
                    continue
                count += 1
                yield normalized

    def _depth_for(self, max_results: int) -> int:
        """Scroll only as deep as the result cap needs.

        Each scroll step surfaces roughly 7 results. Scrolling deeper than the
        cap is pure waste: the extra results are scraped and then discarded, and
        it is the difference between a 25-second run and a 9-minute one.
        A configured depth above 0 is an explicit override.
        """
        if self.depth > 0:
            return self.depth
        return max(1, min(40, -(-max_results // 7) + 1))

    def _normalize(self, entry: dict, niche: str, location: str) -> dict | None:
        name = (entry.get("title") or "").strip()
        if not name:
            return None

        place_id = entry.get("place_id") or entry.get("cid") or None
        status = (entry.get("status") or "").lower()

        emails = [e for e in (entry.get("emails") or []) if e and "@" in e]

        return {
            # Real Google place ids, so these dedupe cleanly against Places API
            # results rather than colliding by name.
            "place_id": place_id,
            "name": name,
            "category": entry.get("category") or None,
            "address": entry.get("address") or None,
            "city": location,
            "phone": entry.get("phone") or None,
            "website": (entry.get("web_site") or "").strip() or None,
            "rating": entry.get("review_rating") or None,
            "review_count": entry.get("review_count") or None,
            "maps_url": entry.get("link") or None,
            "source": self.name,
            "search_niche": niche,
            "search_location": location,
            "business_status": "CLOSED_PERMANENTLY"
            if "permanently closed" in status
            else None,
            "emails": emails,
        }


def _read_entries(path: Path) -> Iterator[dict]:
    """Output is JSON Lines in some versions and a JSON array in others."""
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return

    if text.startswith("["):
        try:
            for entry in json.loads(text):
                if isinstance(entry, dict):
                    yield entry
            return
        except json.JSONDecodeError:
            pass

    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line or line in ("[", "]"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            yield entry
