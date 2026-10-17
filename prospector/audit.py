"""Website audit: decide whether a business's site is bad enough to pitch.

Three jobs:
  1. Score the site so `draft` has a concrete, true thing to point at.
     "Your site isn't secure and doesn't work on phones" beats "I noticed your
     online presence could be improved."
  2. Record what the site cannot DO for a customer — booking, quotes, service
     information, a way to make contact. That is opportunity.py's job, kept in
     its own module because a missing booking button is not a defect and must
     never be pitched as one.
  3. Harvest a contact email, since Google never provides one.

Everything here is read-only HTTP plus one headless page render. Nothing is
submitted, nothing is logged in to.
"""

from __future__ import annotations

import json
import re
import ssl
import time
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from . import opportunity

# Points per problem. A site needs `qualify.bad_site_threshold` total to count
# as a lead. Weights are tuned so any single severe defect (dead site, no
# HTTPS + no mobile) clears a threshold of 4 on its own.
ISSUE_WEIGHTS: dict[str, int] = {
    "unreachable": 5,
    "cert_expired": 4,
    "cert_untrusted": 4,
    "cert_hostname": 4,
    # Ambiguous on its own: often the server omitting intermediates rather than
    # a broken cert, and some browsers recover. Too weak to qualify a lead alone.
    "cert_chain": 2,
    "not_mobile_friendly": 4,
    "no_https": 3,
    "mobile_overflow": 3,
    "very_slow": 3,
    "legacy_layout": 3,
    "slow": 2,
    "stale_copyright": 2,
    "thin_content": 2,
    "parked_or_placeholder": 4,
    "no_title": 1,
    "no_meta_description": 1,
}

# Human phrasing for the drafting step. Keep these factual and unarguable.
ISSUE_TEXT: dict[str, str] = {
    "unreachable": "the site listed on their Google profile does not load at all",
    "cert_expired": "their SSL certificate has expired, so visitors get a full-page security warning",
    "cert_untrusted": "the SSL certificate is self-signed or from an untrusted issuer, so browsers show a security warning",
    "cert_hostname": "the SSL certificate does not match the domain, so browsers show a security warning",
    "cert_chain": "the server does not send its full certificate chain, so some browsers and older devices show a security warning",
    "not_mobile_friendly": "the site has no mobile viewport, so it renders desktop-sized on phones",
    "no_https": "the site still runs on http, so Chrome labels it 'Not secure'",
    "mobile_overflow": "the layout overflows sideways on a phone screen",
    "very_slow": "the homepage takes over 8 seconds to load",
    "slow": "the homepage takes over 4 seconds to load",
    "legacy_layout": "the page is built on table/frameset layout from a much older era of the web",
    "stale_copyright": "the footer copyright is years out of date",
    # No longer emitted: "no way to contact them" is a commercial gap, scored
    # in opportunity.py. Kept so audits recorded before that split still read.
    "no_contact_info": "there is no clickable phone number or email on the homepage",
    "thin_content": "the homepage is nearly empty",
    "parked_or_placeholder": "the domain shows a parking or 'coming soon' placeholder instead of a real site",
    "no_title": "the page has no title tag",
    "no_meta_description": "the page has no meta description",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
YEAR_RE = re.compile(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(\d{4})", re.I)

CONTACT_PATHS = ("/contact", "/contact-us", "/contact.html", "/about", "/about-us")

# These mean a WAF or bot filter stopped US, not that the site is broken. Real
# visitors load these sites fine, so a finding here would be a false claim.
BLOCKED_STATUSES = {401, 403, 405, 406, 429, 451, 503}

PLACEHOLDER_MARKERS = (
    "coming soon",
    "under construction",
    "this domain is for sale",
    "buy this domain",
    "parked free",
    "godaddy.com/domainsearch",
    "default web site page",
    "welcome to nginx",
    "apache2 ubuntu default page",
    "future home of something quite cool",
)

# Matched against the domain exactly, not as a substring: "example.com" as a
# substring would also reject a real address at "acme-example.com".
JUNK_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "domain.com",
    "yourdomain.com",
    "email.com",
    "sentry.io",
    "sentry.wixpress.com",
    "wixpress.com",
    "godaddy.com",
    "squarespace.com",
    "wix.com",
}

# Emails scraped out of asset filenames and template boilerplate.
JUNK_LOCAL_PARTS = {"your", "youremail", "email", "user", "name", "someone", "info@example"}

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico")


class Auditor:
    """Reuses one HTTP client and one browser across a whole batch."""

    def __init__(self, cfg) -> None:
        self.timeout = int(cfg.get("audit.timeout_seconds", 15))
        self.use_playwright = bool(cfg.get("audit.use_playwright", True))
        self.user_agent = cfg.get(
            "audit.user_agent", "Mozilla/5.0 (compatible; SiteAuditBot/1.0)"
        )
        self.stale_before = datetime.now().year - 3
        self._client: httpx.Client | None = None
        self._pw = None
        self._browser = None

    # ------------------------------------------------------------------ setup

    def __enter__(self) -> "Auditor":
        self._client = httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": self.user_agent},
            verify=_ssl_context(),
        )
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._client:
            self._client.close()
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass

    def _browser_page(self):
        """Lazily start Chromium; a run with no reachable sites never pays for it."""
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        return self._browser.new_page(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2,
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            ),
        )

    # ------------------------------------------------------------------- audit

    def audit(self, website: str | None) -> dict[str, Any]:
        """Audit one site. Returns a row ready for db.save_audit, plus 'emails'."""
        result: dict[str, Any] = {
            "reachable": 0,
            "final_url": None,
            "https": 0,
            "status_code": None,
            "load_ms": None,
            "mobile_friendly": None,
            "copyright_year": None,
            "page_title": None,
            "blocked": 0,
            "issues_json": "[]",
            "score": 0,
            "headline_issue": None,
            "emails": [],
            # Unknown until a page is parsed; a site we never saw gets no
            # commercial findings invented for it.
            **opportunity.unknown(),
        }
        if not website:
            # No site at all is not "unknown" — it is every capability missing.
            result.update(opportunity.no_website())
            return result

        url = _normalize_url(website)
        issues: list[str] = []

        response, elapsed_ms, transport_error = self._fetch(url)

        if response is None:
            issues.append(
                transport_error
                if transport_error and transport_error.startswith("cert_")
                else "unreachable"
            )
            result["issues_json"] = json.dumps(issues)
            result["score"] = _score(issues)
            result["headline_issue"] = _headline(issues)
            return result

        result["reachable"] = 1
        result["final_url"] = str(response.url)
        result["status_code"] = response.status_code
        result["load_ms"] = elapsed_ms
        result["https"] = 1 if str(response.url).startswith("https://") else 0

        if response.status_code in BLOCKED_STATUSES:
            # The body is the WAF's page, not theirs. Parsing it would invent
            # findings about a site we never actually saw.
            result["blocked"] = 1
            return result

        if transport_error == "cert_chain":
            issues.append("cert_chain")
        if not result["https"]:
            issues.append("no_https")
        if response.status_code >= 400:
            issues.append("unreachable")
        if elapsed_ms > 8000:
            issues.append("very_slow")
        elif elapsed_ms > 4000:
            issues.append("slow")

        html = response.text or ""
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ", strip=True)

        title = (soup.title.string or "").strip() if soup.title and soup.title.string else None
        result["page_title"] = title
        if not title:
            issues.append("no_title")
        if not soup.find("meta", attrs={"name": "description"}):
            issues.append("no_meta_description")

        if not soup.find("meta", attrs={"name": "viewport"}):
            issues.append("not_mobile_friendly")

        lowered = (title or "").lower() + " " + text[:2000].lower()
        if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
            issues.append("parked_or_placeholder")

        if len(text) < 400:
            issues.append("thin_content")

        if soup.find("frameset") or len(soup.find_all("table")) >= 3 and not soup.find("main"):
            issues.append("legacy_layout")

        year_match = YEAR_RE.search(html)
        if year_match:
            year = int(year_match.group(1))
            result["copyright_year"] = year
            if year < self.stale_before:
                issues.append("stale_copyright")

        # Mobile render check. Only worth it if the site is actually up.
        if self.use_playwright and response.status_code < 400:
            mobile = self._mobile_check(str(response.url))
            if mobile is not None:
                result["mobile_friendly"] = 1 if mobile else 0
                if not mobile and "not_mobile_friendly" not in issues:
                    issues.append("mobile_overflow")
        elif "not_mobile_friendly" in issues:
            result["mobile_friendly"] = 0

        result.update(opportunity.analyse(soup, html, str(response.url)))

        result["emails"] = self._harvest_emails(soup, html, str(response.url))

        issues = list(dict.fromkeys(issues))  # dedupe, keep order
        result["issues_json"] = json.dumps(issues)
        result["score"] = _score(issues)
        result["headline_issue"] = _headline(issues)
        return result

    # ---------------------------------------------------------------- internals

    def _fetch(self, url: str) -> tuple[httpx.Response | None, int, str | None]:
        assert self._client is not None
        started = time.perf_counter()
        try:
            resp = self._client.get(url)
            return resp, int((time.perf_counter() - started) * 1000), None
        except Exception as exc:
            kind = _classify_transport_error(exc)
            if kind == "cert_chain":
                # Not confidently the site's fault, so keep auditing the content
                # rather than throwing away every other signal about the page.
                try:
                    with httpx.Client(
                        timeout=self.timeout,
                        follow_redirects=True,
                        verify=False,
                        headers={"User-Agent": self.user_agent},
                    ) as insecure:
                        resp = insecure.get(url)
                    return resp, int((time.perf_counter() - started) * 1000), "cert_chain"
                except Exception:
                    return None, 0, "dead"
            return None, 0, kind

    def _mobile_check(self, url: str) -> bool | None:
        """True if the page fits a phone screen without sideways scrolling."""
        try:
            page = self._browser_page()
        except Exception:
            return None
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
            page.wait_for_timeout(1200)
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - window.innerWidth"
            )
            return bool(overflow is not None and overflow <= 8)
        except Exception:
            return None
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _harvest_emails(self, soup: BeautifulSoup, html: str, base_url: str) -> list[str]:
        found: list[tuple[str, str]] = []

        for link in soup.select('a[href^="mailto:"]'):
            addr = (link.get("href") or "")[7:].split("?")[0]
            if _valid_email(addr):
                found.append((addr.lower(), base_url))

        for match in EMAIL_RE.findall(html):
            if _valid_email(match):
                found.append((match.lower(), base_url))

        if not found and self._client:
            for path in CONTACT_PATHS:
                try:
                    resp = self._client.get(urljoin(base_url, path))
                    if resp.status_code >= 400:
                        continue
                    for match in EMAIL_RE.findall(resp.text or ""):
                        if _valid_email(match):
                            found.append((match.lower(), str(resp.url)))
                    if found:
                        break
                except Exception:
                    continue

        seen: dict[str, str] = {}
        for addr, source in found:
            seen.setdefault(addr, source)
        return [{"email": a, "found_on": s} for a, s in seen.items()]


# ------------------------------------------------------------------- functions


def _ssl_context():
    """Verify certificates the way a visitor's browser does.

    Python's bundled CA set lags the OS, and a gap there shows up as
    CERTIFICATE_VERIFY_FAILED on sites that every real browser loads fine. Since
    the whole point of this check is "would a visitor see a security warning?",
    the OS trust store is the correct authority. Falls back to httpx's default.
    """
    try:
        import ssl as _ssl

        import truststore

        return truststore.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return True


def _classify_transport_error(exc: Exception) -> str:
    """Never report a client-side trust gap as the site's broken certificate."""
    msg = str(exc).lower()
    if "expired" in msg:
        return "cert_expired"
    if "name does not match" in msg or "hostname mismatch" in msg:
        return "cert_hostname"
    if "self signed" in msg or "self-signed" in msg or "not trusted" in msg or "unknown ca" in msg:
        return "cert_untrusted"
    if "unable to get local issuer" in msg or "unable to verify" in msg:
        return "cert_chain"
    if "certificate" in msg or "ssl" in msg or "tlsv1" in msg:
        return "cert_untrusted"
    return "dead"


def _normalize_url(website: str) -> str:
    website = website.strip()
    if not website.startswith(("http://", "https://")):
        website = "https://" + website
    return website


def _valid_email(addr: str) -> bool:
    addr = addr.strip().lower().rstrip(".,;:")
    if not addr or addr.count("@") != 1 or len(addr) > 100:
        return False
    if not EMAIL_RE.fullmatch(addr):
        return False

    local, domain = addr.split("@")
    if domain in JUNK_EMAIL_DOMAINS or local in JUNK_LOCAL_PARTS:
        return False
    # "logo@2x.png" and friends look like addresses to a regex.
    if domain.endswith(IMAGE_SUFFIXES) or local.endswith(IMAGE_SUFFIXES):
        return False
    if "@2x" in addr or "@3x" in addr:
        return False
    return True


def _score(issues: list[str]) -> int:
    return sum(ISSUE_WEIGHTS.get(issue, 1) for issue in issues)


def _headline(issues: list[str]) -> str | None:
    """The single most pitchable defect, for the drafting prompt."""
    if not issues:
        return None
    worst = max(issues, key=lambda i: ISSUE_WEIGHTS.get(i, 1))
    return ISSUE_TEXT.get(worst, worst)


def describe_issues(issues_json: str | None) -> list[str]:
    try:
        issues = json.loads(issues_json or "[]")
    except json.JSONDecodeError:
        return []
    return [ISSUE_TEXT.get(i, i) for i in issues]


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urlparse(_normalize_url(url)).netloc.lower().removeprefix("www.")
    except Exception:
        return None
