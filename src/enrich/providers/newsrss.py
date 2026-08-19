"""'newsrss' provider: Google News RSS appointment announcements. Free, keyless.

Identify stage only, and ONLY for roles legal/coo/cfo. Heuristic: press
releases are the best free source for GC/COO/CFO appointments ("Acme appoints
Jane Smith as General Counsel"), while a small company's CEO is better found
by site scrape or SERP — their tenure usually predates any coverage. Noise is
worse than nothing here, so extraction is deliberately conservative and this
provider returns [] readily.

Feed shape (verified 2026-08-19):
    GET https://news.google.com/rss/search?q=<urlencoded query>&hl=en-US&gl=US&ceid=US:en
    -> RSS 2.0: rss/channel/item{title, link, pubDate, description, source}
    Item titles end with " - Publisher".

The feed is XML, not JSON, so the fetch wraps the body as {"xml": <text>} to
stay compatible with the SQLite JSON cache (net.fetch, not net.fetch_json).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .. import net
from ..models import ROLE_TITLES, Company, title_matches_role
from .base import PersonHit, ProviderBase, register

RSS_URL = "https://news.google.com/rss/search"
NEWS_ROLES = ("legal", "coo", "cfo")   # see module docstring for the why

_LEGAL_SUFFIXES = {"inc", "llc", "ltd", "co", "corp", "corporation", "company", "lp", "llp", "plc"}
_NAME_STOPWORDS = {
    "new", "its", "the", "as", "to", "general", "chief", "counsel", "officer", "interim",
    "former", "veteran", "senior", "executive", "global", "head", "president", "first",
    "incoming", "next", "company", "group",
}

# Person name: 2-4 capitalized tokens; lazy so trailing Title-Case words
# ("... Jane Smith As General Counsel") fall through to the filler/title parts.
_NAME = r"(?P<name>[A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*){1,3}?)"
_FILLER = r"(?i:(?:its|the|a|an|new|first|next|incoming)\s+)*"

_PATTERNS: dict[str, list[re.Pattern[str]]] = {}


def _title_alt(role: str) -> str:
    return "(?P<title>(?i:" + "|".join(re.escape(p) for p in ROLE_TITLES.get(role, [])) + "))"


def _patterns_for(role: str) -> list[re.Pattern[str]]:
    pats = _PATTERNS.get(role)
    if pats is None:
        talt = _title_alt(role)
        verb = r"(?i:appoints|names|hires|taps|promotes)"
        pats = [
            # "Acme Appoints Jane Smith as General Counsel"
            re.compile(rf"\b{verb}\s+{_NAME}\s+(?i:(?:as|to)\s+)?{_FILLER}{talt}"),
            # "Jane Smith Joins Acme as General Counsel"
            re.compile(rf"{_NAME}\s+(?i:joins)\s+.*?\s(?i:as)\s+{_FILLER}{talt}"),
            # "Jane Smith Named/Appointed General Counsel of Acme"
            re.compile(rf"{_NAME}\s+(?i:named|appointed)\s+(?i:as\s+)?{_FILLER}{talt}"),
        ]
        _PATTERNS[role] = pats
    return pats


def _norm_text(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split())


def _company_in_text(company: Company, text: str) -> bool:
    t = _norm_text(text)
    toks = _norm_text(company.name).split()
    while toks and toks[-1] in _LEGAL_SUFFIXES:
        toks.pop()
    if toks and " ".join(toks) in t:
        return True
    stem = (company.domain or company.email_domain or "").split(".")[0].lower()
    if stem and len(stem) >= 4 and stem in t.replace(" ", ""):
        return True
    return False


def _looks_like_person(name: str) -> bool:
    parts = [p for p in name.replace(",", " ").split() if p]
    if not (2 <= len(parts) <= 4):
        return False
    for p in parts:
        if not p[0].isupper() or any(ch.isdigit() for ch in p):
            return False
        if p.lower().strip(".") in _NAME_STOPWORDS:
            return False
    return True


def _items(xml_text: str) -> list[tuple[str, str]]:
    """[(item title, article link)] — empty on malformed XML."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if title:
            out.append((title, link))
    return out


def _extract(headline: str, role: str) -> tuple[str, str]:
    for pat in _patterns_for(role):
        m = pat.search(headline)
        if m:
            return m.group("name").strip(), m.group("title").strip()
    return "", ""


@register
class NewsRss(ProviderBase):
    name = "newsrss"
    key_env = None
    is_free = True
    rps = 1.0

    async def _rss(self, q: str) -> dict:
        async def fetch():
            resp = await net.fetch(
                self.ctx.http,
                "GET",
                RSS_URL,
                params={"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"},
            )
            return {"xml": resp.text}

        return await self.cached_json("rss_search", {"q": q}, fetch, cost_usd=0.0)

    def _query(self, company: Company, role: str) -> str:
        phrases = ROLE_TITLES[role]
        # add the acronym for roles that headline as "COO"/"CFO"
        if len(phrases) > 1 and len(phrases[1]) <= 4:
            return f'"{company.name}" ("{phrases[0]}" OR "{phrases[1].upper()}")'
        return f'"{company.name}" "{phrases[0]}"'

    async def identify(self, company: Company, role: str) -> list[PersonHit]:
        if role not in NEWS_ROLES or not company.name:
            return []
        data = await self._rss(self._query(company, role))

        hits: dict[str, PersonHit] = {}
        for item_title, link in _items(data.get("xml") or ""):
            headline = item_title.rsplit(" - ", 1)[0].strip()   # drop " - Publisher"
            if not _company_in_text(company, headline):
                continue
            name, matched_title = _extract(headline, role)
            if not name or not _looks_like_person(name):
                continue
            tscore = title_matches_role(matched_title, role)
            if tscore == 0.0:
                continue
            conf = round(min(0.6, 0.5 + 0.1 * tscore), 2)
            key = _norm_text(name)
            prev = hits.get(key)
            if prev is None or conf > prev.confidence:
                hits[key] = PersonHit(
                    name=name,
                    title=matched_title,
                    role=role,
                    source=self.name,
                    url=link,
                    confidence=conf,
                    raw={"headline": item_title},
                )
        return list(hits.values())

    async def doctor(self) -> str:
        data = await self._rss('"general counsel"')
        n = len(_items(data.get("xml") or ""))
        return f"ok (keyless) - rss reachable, {n} items for test query"
