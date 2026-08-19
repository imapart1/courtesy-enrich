"""Exa.ai adapter — neural web search for the long-tail identify stage (Stage 2 step 5).

Endpoint: POST https://api.exa.ai/search with type "auto" (the 2026 API replaced the
old "neural"/"keyword" types with instant/fast/auto/deep-*). Page text must be
requested explicitly via the `contents` parameter but is included in the flat
per-search price. Verified against https://exa.ai/docs/reference/search 2026-08-19.

Exa has no public account/quota endpoint (balance lives on the dashboard billing
page only), so doctor() runs a 1-result search — it costs one search (~$0.007).

Extraction is deliberately conservative: SERP-grade evidence only supports
confidence 0.55-0.75, and returning [] beats returning a hallucinated name.
"""

from __future__ import annotations

import re
from collections import Counter
from functools import cache

from .. import net
from ..models import ROLE_TITLES, Company, title_matches_role
from .base import CompanyInfo, PersonHit, ProviderBase, ProviderError, register

BASE_URL = "https://api.exa.ai"
SEARCH_TYPE = "auto"
DEFAULT_COST_PER_SEARCH_USD = 0.007  # $7/1k requests — exa.ai/pricing + docs/service-research.md
COST_ENV = "EXA_COST_PER_SEARCH"     # override per-search price; free-credit users can set 0
TEXT_MAX_CHARS = 2000

# --------------------------------------------------------------------------- parsing

_NAME_TOKEN = r"[A-Z][a-zA-Z'’\-]+"
_NAME_RE = rf"{_NAME_TOKEN}(?:\s+(?:[A-Z]\.|{_NAME_TOKEN})){{1,3}}"

# Capitalized words that make a "name" clearly not a person.
_NON_NAME_TOKENS = {
    "the", "a", "an", "our", "new", "legal", "law", "counsel", "general", "chief",
    "officer", "executive", "president", "vice", "privacy", "compliance", "operations",
    "finance", "financial", "department", "team", "office", "linkedin", "company",
    "group", "holdings", "inc", "llc", "corp", "corporation", "director", "head",
    "senior", "about", "contact", "board", "staff", "leadership", "management",
}

_VERBS = (
    r"is|was|becomes|became|serves\s+as|now\s+serves\s+as|will\s+serve\s+as|"
    r"has\s+been\s+named|was\s+named|named|has\s+been\s+appointed|was\s+appointed|"
    r"appointed|has\s+been\s+promoted\s+to|promoted\s+to|joins\s+as|joined\s+as|as|to"
)
_ARTICLES = r"the|our|its|their|a|an|new|current|interim|acting|company's"

_FORMER_RE = re.compile(r"(?i)\b(former|ex|outgoing|previous|retired|late|then)[\s\-]*$")

_COMPANY_STOP = {"the", "inc", "llc", "corp", "corporation", "company", "and", "co", "ltd"}


@cache
def _patterns_for_role(role: str) -> tuple[re.Pattern[str], re.Pattern[str]] | None:
    phrases = ROLE_TITLES.get(role)
    if not phrases:
        return None
    titles = "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True))
    name_first = re.compile(
        rf"(?P<name>{_NAME_RE})"
        rf"(?:\s*[,:–—-]\s*|\s+)"
        rf"(?i:(?:{_VERBS})\s+)?"
        rf"(?i:(?:{_ARTICLES})\s+){{0,2}}"
        rf"(?i:(?P<title>{titles}))(?![a-zA-Z])"
    )
    # Org segment after the title intentionally disallows '.'/',' so a sentence
    # boundary stops the match instead of bleeding into the next capitalized words.
    title_first = re.compile(
        rf"(?i:(?P<title>{titles}))(?![a-zA-Z])"
        rf"(?:\s+(?:of|at|for)\s+[A-Z0-9][\w&'’ -]{{0,50}}?)?"
        rf"[,:\s]+"
        rf"(?i:is\s+(?:now\s+)?)?"
        rf"(?P<name>{_NAME_RE})"
    )
    return name_first, title_first


def _looks_like_person(name: str) -> bool:
    tokens = name.split()
    if not (2 <= len(tokens) <= 4):
        return False
    return not any(t.lower().strip(".,") in _NON_NAME_TOKENS for t in tokens)


def _parse_linkedin_title(result_title: str) -> tuple[str, str] | None:
    """'Jane Smith - General Counsel - Acme | LinkedIn' -> ('Jane Smith', 'General Counsel')."""
    t = re.sub(r"\s*[|·]\s*LinkedIn.*$", "", result_title.strip(), flags=re.I)
    parts = re.split(r"\s+[-–—|]\s+|,\s+", t)
    if len(parts) < 2:
        return None
    name, title = " ".join(parts[0].split()), parts[1].strip()
    if not title or not re.fullmatch(_NAME_RE, name) or not _looks_like_person(name):
        return None
    return name, title


def _extract_people(text: str, role: str) -> list[tuple[str, str, str]]:
    """Conservative (name, title, evidence-snippet) extraction from page text."""
    pats = _patterns_for_role(role)
    if not pats or not text:
        return []
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for pat in pats:
        for m in pat.finditer(text):
            if _FORMER_RE.search(text[max(0, m.start() - 24):m.start()]):
                continue
            name = " ".join(m.group("name").split())
            if not _looks_like_person(name) or name.lower() in seen:
                continue
            seen.add(name.lower())
            snippet = " ".join(text[max(0, m.start() - 60):m.end() + 60].split())[:200]
            found.append((name, m.group("title"), snippet))
    return found


def _company_tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) >= 3 and t not in _COMPANY_STOP]


# ---------------------------------------------------------------- acquisition parsing

_ACQ_NAME = r"[A-Z][\w&'’.-]*(?:\s+[A-Z][\w&'’.-]*){0,4}"
_ACQ_HINTS = (
    "acquir", "parent", "subsidiar", "merger", "merged", "bought",
    "owned by", "holding", "division of", "brand of",
)


def _acquirer_patterns(company_name: str) -> list[re.Pattern[str]]:
    cname = re.escape(company_name.strip())
    return [
        re.compile(
            rf"(?i:{cname}\s+(?:(?:was|has\s+been|is\s+being|is|will\s+be)\s+)?acquired\s+by)"
            rf"\s+(?:(?i:the)\s+)?(?P<acq>{_ACQ_NAME})"
        ),
        re.compile(
            rf"(?P<acq>{_ACQ_NAME})\s+"
            rf"(?i:(?:acquires|acquired|has\s+acquired|to\s+acquire|buys|bought|"
            rf"completes\s+(?:its\s+)?acquisition\s+of|announced\s+(?:the|its)\s+acquisition\s+of)"
            rf"\s+{cname})"
        ),
        re.compile(
            rf"(?i:{cname},?\s+(?:is\s+|became\s+)?(?:now\s+)?a\s+(?:wholly[\s-]owned\s+)?"
            rf"(?:subsidiary|division|unit|brand)\s+of)\s+(?P<acq>{_ACQ_NAME})"
        ),
        re.compile(
            rf"(?i:{cname}(?:'s|’s)?\s+parent(?:\s+company)?(?:\s+is|,)?)\s+(?P<acq>{_ACQ_NAME})"
        ),
    ]


def _clean_company_name(raw: str) -> str:
    cand = " ".join(raw.split()).strip(" ,.;:'’-")
    cand = re.sub(r"(?:'s|’s)$", "", cand)
    if len(cand) < 3 or cand.lower() in {"the", "its", "this", "that", "they"}:
        return ""
    return cand


def _extract_acquirer(results: list[dict], company: Company) -> tuple[str, str, str] | None:
    """Most-corroborated acquirer/parent -> (name, evidence_url, evidence). None is expected."""
    pats = _acquirer_patterns(company.name)
    norm_company = " ".join(re.findall(r"[a-z0-9]+", company.name.lower()))
    counts: Counter[str] = Counter()
    evidence: dict[str, tuple[str, str]] = {}
    for r in results:
        text = f"{r.get('title') or ''}\n{r.get('text') or ''}"
        url = r.get("url") or ""
        for pat in pats:
            for m in pat.finditer(text):
                cand = _clean_company_name(m.group("acq"))
                if not cand or " ".join(re.findall(r"[a-z0-9]+", cand.lower())) == norm_company:
                    continue
                counts[cand] += 1
                evidence.setdefault(cand, (url, " ".join(text[max(0, m.start() - 40):m.end() + 40].split())[:200]))
    if not counts:
        return None
    winner, _ = counts.most_common(1)[0]
    url, snip = evidence[winner]
    return winner, url, snip


# --------------------------------------------------------------------------- provider

@register
class Exa(ProviderBase):
    name = "exa"
    key_env = "EXA_API_KEY"
    is_free = False
    rps = 5.0

    @property
    def cost_per_search(self) -> float:
        raw = self.ctx.settings.key(COST_ENV)
        if raw is None:
            return DEFAULT_COST_PER_SEARCH_USD
        try:
            return float(raw)
        except ValueError as e:
            raise ProviderError(f"{COST_ENV} must be a number, got {raw!r}") from e

    async def _search(
        self, query: str, *, num_results: int = 5, text: bool = True, op: str = "search"
    ) -> dict:
        if not self.api_key:
            raise ProviderError("EXA_API_KEY is not configured")
        body: dict = {"query": query, "type": SEARCH_TYPE, "numResults": num_results}
        if text:
            body["contents"] = {"text": {"maxCharacters": TEXT_MAX_CHARS}}
        params = {"query": query, "type": SEARCH_TYPE, "numResults": num_results, "text": text}

        async def _fetch():
            if self.ctx.http is None:
                raise ProviderError("Ctx not entered: no HTTP client")
            return await net.fetch_json(
                self.ctx.http, "POST", f"{BASE_URL}/search",
                headers={"x-api-key": self.api_key},
                json_body=body,
            )

        # Reserve/charge the flat estimate up front so the budget cap is respected
        # pre-call (the real per-call price is unknown until the response arrives).
        estimate = self.cost_per_search
        fresh = self.ctx.store.cache_get(
            self.name, op, params, self.ctx.settings.cache_ttl_days
        ) is None
        data = await self.cached_json(op, params, _fetch, cost_usd=estimate, credits=1.0)

        # On a FRESH call, rewrite the ledger row with Exa's authoritative per-call
        # price from costDollars.total (same INSERT-OR-REPLACE miss-rewrite hunter uses).
        # Skip when the user pinned the price via EXA_COST_PER_SEARCH — that override is
        # their real money cost (e.g. 0 on free credits) and wins over the list price.
        if fresh and self.ctx.settings.key(COST_ENV) is None:
            actual = ((data or {}).get("costDollars") or {}).get("total")
            if actual is not None:  # guard: missing/None -> keep the flat estimate
                self.ctx.store.cache_put(
                    self.name, op, params, data,
                    cost_usd=float(actual), credits=1, run_id=self.ctx.run_id,
                )
        return data

    # ------------------------------------------------------------------ identify

    async def identify(self, company: Company, role: str) -> list[PersonHit]:
        if not company.name or role not in ROLE_TITLES:
            return []
        top_title = ROLE_TITLES[role][0].title()
        serp = await self._search(f"{company.name} {top_title} site:linkedin.com/in")
        hits = self._hits_from_results(serp, company, role)
        if not any(h.confidence >= 0.70 for h in hits):
            where = f" ({company.domain})" if company.domain else ""
            neural = await self._search(f"who is the {top_title} of {company.name}{where}?")
            hits += self._hits_from_results(neural, company, role)
        best: dict[str, PersonHit] = {}
        for h in hits:
            k = h.name.lower()
            if k not in best or h.confidence > best[k].confidence:
                best[k] = h
        return sorted(best.values(), key=lambda h: -h.confidence)

    def _hits_from_results(self, data: dict, company: Company, role: str) -> list[PersonHit]:
        out: list[PersonHit] = []
        ctoks = _company_tokens(company.name)
        for r in (data or {}).get("results") or []:
            url = r.get("url") or ""
            rtitle = r.get("title") or ""
            text = r.get("text") or ""
            blob = f"{rtitle}\n{url}\n{text}".lower()
            if ctoks and not any(t in blob for t in ctoks):
                continue  # evidence never mentions the company — do not trust it
            if "linkedin.com/in" in url:
                parsed = _parse_linkedin_title(rtitle)
                if parsed:
                    pname, ptitle = parsed
                    score = title_matches_role(ptitle, role)
                    if score > 0:
                        out.append(PersonHit(
                            name=pname, title=ptitle, role=role, source=self.name, url=url,
                            confidence=round(min(0.75, 0.55 + 0.2 * score), 2),
                            raw={"result_title": rtitle},
                        ))
                        continue
            for pname, ptitle, snippet in _extract_people(f"{rtitle}\n{text}", role):
                score = title_matches_role(ptitle, role)
                if score <= 0:
                    continue
                out.append(PersonHit(
                    name=pname, title=ptitle, role=role, source=self.name, url=url,
                    confidence=round(min(0.75, 0.45 + 0.2 * score), 2),
                    raw={"evidence": snippet},
                ))
        return out

    # -------------------------------------------------------------- company_info

    async def company_info(self, company: Company) -> CompanyInfo | None:
        """Single acquisition/parent lookup — only when notes/name already hint at one."""
        if not company.name:
            return None
        hint_blob = f"{company.name} {company.notes}".lower()
        if not any(h in hint_blob for h in _ACQ_HINTS):
            return None
        data = await self._search(f"{company.name} acquired by OR parent company", num_results=3)
        found = _extract_acquirer((data or {}).get("results") or [], company)
        if not found:
            return None
        parent, url, snippet = found
        return CompanyInfo(
            parent_company=parent, source=self.name,
            raw={"evidence_url": url, "evidence": snippet},
        )

    # -------------------------------------------------------------------- doctor

    async def doctor(self) -> str:
        """Exa exposes no quota endpoint; cheapest live check is a 1-result search."""
        if not self.api_key:
            return "not configured - set EXA_API_KEY in .env"
        try:
            data = await self._search("Exa AI web search API", num_results=1, text=False, op="doctor")
        except net.HttpError as e:
            return f"error - HTTP {e.status} from POST /search"
        n = len((data or {}).get("results") or [])
        cost = float(((data or {}).get("costDollars") or {}).get("total") or self.cost_per_search)
        return (
            f"ok - key accepted, {n} result from 1-result test search "
            f"(this check cost ${cost:.4f}; Exa has no free quota endpoint)"
        )
