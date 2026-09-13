"""Turning a scraped phone number into something safe to message.

This used to be hand-rolled: strip the trunk zero, bolt on a country code, check
the length. Adversarial testing killed it. Real numbering plans do not share one
rule — Italy keeps its leading zero internationally, Russia's trunk prefix is 8,
Hungary's is 06, and Mexico's legacy 01 prefix survives as "+52 1…", which is
WhatsApp's marker for a MOBILE. That last one is the reason this file now
delegates: the hand-rolled version turned a Mexico City landline into a
perfectly valid WhatsApp address belonging to a different person.

Google's libphonenumber knows every plan and is kept current. The only thing
left here is deciding which country a lead is in, and refusing to guess.
"""

from __future__ import annotations

import re

import phonenumbers
from phonenumbers import PhoneNumberFormat, PhoneNumberType

# Country / region names as they appear in a Google Maps location string,
# mapped to ISO 3166-1 alpha-2, which is what libphonenumber wants.
REGION_NAMES: dict[str, str] = {
    "united states": "US", "usa": "US", "u.s.a": "US", "u.s.": "US", "america": "US",
    "canada": "CA",
    "united kingdom": "GB", "great britain": "GB", "britain": "GB", "uk": "GB",
    "u.k.": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "northern ireland": "GB",
    "ireland": "IE", "republic of ireland": "IE",
    "united arab emirates": "AE", "uae": "AE", "u.a.e": "AE", "emirates": "AE",
    "dubai": "AE", "abu dhabi": "AE", "sharjah": "AE", "ajman": "AE",
    "fujairah": "AE", "ras al khaimah": "AE", "umm al quwain": "AE",
    "saudi arabia": "SA", "ksa": "SA", "qatar": "QA", "kuwait": "KW",
    "bahrain": "BH", "oman": "OM", "jordan": "JO", "lebanon": "LB",
    "egypt": "EG", "morocco": "MA", "tunisia": "TN", "algeria": "DZ",
    "india": "IN", "bharat": "IN", "pakistan": "PK", "bangladesh": "BD",
    "sri lanka": "LK", "nepal": "NP",
    "philippines": "PH", "indonesia": "ID", "malaysia": "MY", "singapore": "SG",
    "thailand": "TH", "vietnam": "VN", "viet nam": "VN", "cambodia": "KH",
    "australia": "AU", "new south wales": "AU", "victoria": "AU",
    "queensland": "AU", "western australia": "AU", "south australia": "AU",
    "tasmania": "AU", "new zealand": "NZ",
    "south africa": "ZA", "nigeria": "NG", "kenya": "KE", "ghana": "GH",
    "tanzania": "TZ", "uganda": "UG", "ethiopia": "ET",
    "germany": "DE", "deutschland": "DE", "france": "FR", "spain": "ES",
    "españa": "ES", "espana": "ES", "italy": "IT", "italia": "IT",
    "portugal": "PT", "netherlands": "NL", "nederland": "NL", "holland": "NL",
    "belgium": "BE", "switzerland": "CH", "suisse": "CH", "schweiz": "CH",
    "austria": "AT", "österreich": "AT", "sweden": "SE", "norway": "NO",
    "denmark": "DK", "finland": "FI", "poland": "PL", "polska": "PL",
    "czech republic": "CZ", "czechia": "CZ", "hungary": "HU",
    "romania": "RO", "greece": "GR", "croatia": "HR", "serbia": "RS",
    "turkey": "TR", "türkiye": "TR", "turkiye": "TR", "israel": "IL",
    "russia": "RU", "россия": "RU", "kazakhstan": "KZ", "ukraine": "UA",
    "mexico": "MX", "méxico": "MX", "brazil": "BR", "brasil": "BR",
    "argentina": "AR", "chile": "CL", "colombia": "CO", "peru": "PE",
    "japan": "JP", "日本": "JP", "south korea": "KR", "korea": "KR",
    "china": "CN", "中国": "CN", "hong kong": "HK", "taiwan": "TW",
}

# Multi-word names must be tried before the single words they contain, so
# "New South Wales" cannot be swallowed by "wales".
_NAMES_BY_LENGTH = sorted(REGION_NAMES.items(), key=lambda kv: -len(kv[0]))

_US_STATES = {
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia",
    "ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj",
    "nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn","tx","ut","vt",
    "va","wa","wv","wi","wy","dc",
}


def region_for_location(*candidates: str | None) -> str | None:
    """ISO region for a "City, Country" string. None when it isn't clear.

    Tries each candidate in order — pass the full address first, since Google's
    formatted address ends in the country name and is the most reliable signal.

    The last comma-separated part is checked before the whole string: countries
    come last in an address, and matching anywhere first is how "New England,
    USA" once resolved to the United Kingdom and "Emirates Stadium, London, UK"
    to the United Arab Emirates.
    """
    for candidate in candidates:
        if not candidate:
            continue
        text = candidate.strip().lower()
        if not text:
            continue

        parts = [p.strip() for p in text.split(",") if p.strip()]
        tail = parts[-1] if parts else text

        if tail in REGION_NAMES:
            return REGION_NAMES[tail]
        if len(tail) == 2 and tail in _US_STATES:
            return "US"
        for name, region in _NAMES_BY_LENGTH:
            if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", tail):
                return region

        # Scanning the whole string is only safe when there is nothing but the
        # one component: "Little India, Toronto" is in Canada, and a full-text
        # scan happily reads "india" out of the middle of it.
        if len(parts) <= 1:
            for name, region in _NAMES_BY_LENGTH:
                if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", text):
                    return region
    return None


def to_e164(raw: str | None, region: str | None) -> str | None:
    """Digits for a wa.me link, or None if the number isn't provably valid.

    None is the safe answer. A rejected number costs one lead; a wrong number
    sends a stranger a sales pitch.
    """
    if not raw or not str(raw).strip():
        return None
    try:
        parsed = phonenumbers.parse(str(raw), region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, PhoneNumberFormat.E164).lstrip("+")


_MOBILE_TYPES = {PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE}
_FIXED_TYPES = {PhoneNumberType.FIXED_LINE, PhoneNumberType.VOIP,
                PhoneNumberType.TOLL_FREE, PhoneNumberType.PREMIUM_RATE}


def line_type(e164_digits: str | None) -> str:
    """'mobile', 'landline' or 'unknown'.

    WhatsApp effectively needs a mobile, so a landline in the queue is a lead
    you will never hear from. Worth labelling, not dropping — the number is
    still good for an actual phone call.
    """
    if not e164_digits:
        return "unknown"
    try:
        parsed = phonenumbers.parse("+" + str(e164_digits).lstrip("+"), None)
    except phonenumbers.NumberParseException:
        return "unknown"
    kind = phonenumbers.number_type(parsed)
    if kind in _MOBILE_TYPES:
        return "mobile"
    if kind in _FIXED_TYPES:
        return "landline"
    return "unknown"
