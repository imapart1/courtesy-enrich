"""'serp' provider: Serper.dev Google SERP -> public LinkedIn titles (identify stage).

One search per (company, role): the role's strongest title phrase is queried
against site:linkedin.com/in and names/titles are read from the SERP results
themselves — LinkedIn is never scraped. SERP snippets are secondhand evidence,
so confidence is capped at 0.7.

API (verified 2026-08-19 against serper.dev):
    POST https://google.serper.dev/search
    headers: X-API-KEY: <key>, Content-Type: application/json
    body:    {"q": "...", "num": 10, "gl": "us", "hl": "en"}
    resp:    {"organic": [{"title", "link", "snippet", "position"}, ...], ...}

Pricing: 2,500 free queries at signup, then roughly $0.30-1.00/1k. Default
cost below is $0.001/query; override with SERPER_COST_PER_QUERY in .env
(free-tier users set it to 0).
"""

from __future__ import annotations

import re

from .. import net
from ..models import ROLE_TITLES, Company, normalize_title, title_matches_role
from .base import PersonHit, ProviderBase, register

SEARCH_URL = "https://google.serper.dev/search"
COST_PER_QUERY_USD = 0.001   # paid tier; override via SERPER_COST_PER_QUERY (0 on free tier)
RESULTS_PER_QUERY = 10

_LEGAL_SUFFIXES = {"inc", "llc", "ltd", "co", "corp", "corporation", "company", "lp", "llp", "plc"}
_NAME_STOPWORDS = {
    "new", "its", "the", "general", "chief", "counsel", "officer", "interim", "former",
    "veteran", "senior", "executive", "global", "head", "president", "linkedin",
    "profile", "profiles", "top", "jobs", "hiring",
}


def _norm_text(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split())


def _company_tokens(company: Company) -> list[str]:
    toks = _norm_text(company.name).split()
    while toks and toks[-1] in _LEGAL_SUFFIXES:
        toks.pop()
    return toks


def _company_in_text(company: Company, text: str) -> bool:
    """Company name (sans legal suffix) or its domain stem must appear in the text."""
    t = _norm_text(text)
    toks = _company_tokens(company)
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


def _split_serp_title(raw: str) -> tuple[str, str]:
    """'Jane Smith - General Counsel - Acme Co | LinkedIn' -> ('Jane Smith', 'General Counsel')."""
    t = re.sub(r"\s*[|–—-]\s*LinkedIn\s*$", "", raw.strip(), flags=re.I)
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", t) if p.strip()]
    if not parts:
        return "", ""
    name = parts[0].split(",")[0].strip()
    title = parts[1] if len(parts) > 1 else ""
    return name, title


def _title_from_snippet(snippet: str, role: str) -> str:
    """Longest role phrase present in the snippet ('deputy general counsel' beats 'counsel')."""
    s = " " + normalize_title(snippet) + " "
    for phrase in sorted(ROLE_TITLES.get(role, []), key=len, reverse=True):
        if f" {phrase} " in s:
            return phrase
    return ""


@register
class Serp(ProviderBase):
    name = "serp"
    key_env = "SERPER_API_KEY"
    is_free = False              # free tier exists (2,500 queries) but this can spend money
    rps = 5.0

    def _cost_per_query(self) -> float:
        override = self.ctx.settings.key("SERPER_COST_PER_QUERY")
        if override is None:
            return COST_PER_QUERY_USD
        try:
            return float(override)
        except ValueError:
            return COST_PER_QUERY_USD

    async def _search(self, q: str, num: int = RESULTS_PER_QUERY) -> dict:
        params = {"q": q, "num": num, "gl": "us", "hl": "en"}

        async def fetch():
            return await net.fetch_json(
                self.ctx.http,
                "POST",
                SEARCH_URL,
                headers={"X-API-KEY": self.api_key or "", "Content-Type": "application/json"},
                json_body=params,
            )

        return await self.cached_json("search", params, fetch, cost_usd=self._cost_per_query(), credits=1)

    async def identify(self, company: Company, role: str) -> list[PersonHit]:
        phrases = ROLE_TITLES.get(role, [])
        if not company.name or not phrases:
            return []
        q = f'"{company.name}" "{phrases[0]}" site:linkedin.com/in'
        data = await self._search(q)

        hits: dict[str, PersonHit] = {}
        for item in data.get("organic") or []:
            raw_title = item.get("title") or ""
            snippet = item.get("snippet") or ""
            link = item.get("link") or ""
            name, title = _split_serp_title(raw_title)
            if title:
                base = 0.7          # title straight from the SERP result title
            else:
                title = _title_from_snippet(snippet, role)
                base = 0.62         # snippet-derived title: weaker evidence
            if not name or not title or not _looks_like_person(name):
                continue
            tscore = title_matches_role(title, role)
            if tscore == 0.0:
                continue
            # guard against same-name collisions at other companies
            if not _company_in_text(company, f"{raw_title} {snippet}"):
                continue
            conf = round(max(0.55, min(0.7, base * tscore)), 2)
            key = _norm_text(name)
            prev = hits.get(key)
            if prev is None or conf > prev.confidence:
                hits[key] = PersonHit(
                    name=name,
                    title=title,
                    role=role,
                    source=self.name,
                    url=link,
                    confidence=conf,
                    raw={"serp_title": raw_title, "snippet": snippet},
                )
        return list(hits.values())

    async def doctor(self) -> str:
        cost = self._cost_per_query()
        data = await self._search('site:linkedin.com/in "general counsel"', num=1)
        n = len(data.get("organic") or [])
        return f"ok - test query returned {n} organic result(s) (1 query, ${cost:.4f})"
