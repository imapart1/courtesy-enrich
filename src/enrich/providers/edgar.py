"""'edgar' provider: SEC EDGAR. Keyless, free.

company_info: browse-edgar company search (atom output, filtered to 10-K filers).
If a filer fuzzy-matches the defendant's name (or its known entity/parent name),
the company is public or has a public parent -> revenue hint.
NOTE (verified live 2026-08-19): when the company search matches MULTIPLE filers,
EDGAR's atom output omits conformed names entirely (long-standing SEC bug:
<entry title="ARRAY(0x...)">), so only the single-match shape yields a usable
filer. Ambiguous multi-matches are treated as "no match".

identify: EDGAR full-text search (https://efts.sec.gov/LATEST/search-index — shape
verified live 2026-08-19: Elasticsearch envelope, _id = "{accession}:{filename}",
NO snippet/highlight field exists) restricted to the matched filer's CIK and 8-K
filings mentioning the role's strongest title. The top filings (item 5.02 —
officer changes — first, then newest) are fetched from www.sec.gov/Archives and
officer names extracted ONLY from high-confidence sentences: appointment language
("appointed Jane Smith as General Counsel") and signature blocks
("/s/ Jane Smith ... General Counsel"). Confidence 0.6.

Fast path: when browse-edgar finds no filer (private companies — the normal case
for this queue), identify returns [] without ever touching full-text search.

SEC fair-access policy: an identifying User-Agent is sent on every request and we
stay at 5 req/s (limit is 10).
"""

from __future__ import annotations

import difflib
import re
import xml.etree.ElementTree as ET
from functools import cache

from bs4 import BeautifulSoup

from .. import net
from ..models import ROLE_TITLES, Company, title_matches_role
from .base import CompanyInfo, PersonHit, ProviderBase, ProviderError, register

# SEC requires a User-Agent identifying the caller (app + contact) on every request.
EDGAR_UA = "courtesy-enrich ops@schallertpc.com"
_HEADERS = {"User-Agent": EDGAR_UA}

BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
FTS_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

# EDGAR is free (docs/service-research.md, "Free / near-free methods"). Override with
# EDGAR_COST_PER_UNIT (USD per HTTP call) if a paid proxy ever fronts these requests.
COST_PER_CALL_USD = 0.0
COST_ENV = "EDGAR_COST_PER_UNIT"

MATCH_THRESHOLD = 0.85
MAX_DOCS_PER_ROLE = 3
MAX_DOC_TEXT = 150_000

# Role phrases too generic/private-company-flavored to query or trust inside filings.
_WEAK_PHRASES = {
    "legal counsel", "senior counsel", "corporate counsel", "managing member",
    "managing partner", "managing director", "vp legal", "vp of legal", "vp finance",
    "vp of finance", "vp operations", "vp of operations",
}

_SUFFIX_WORDS = r"\b(?:incorporated|corporation|company|holdings?|group|brands?|inc|corp|llc|ltd|lp|llp|plc|co|the)\b"

_NAME_PART = r"(?:[A-Z][A-Za-z'’.\-]+\s+){1,2}[A-Z][A-Za-z'’\-]+"
_NAME_STOP_WORDS = {
    "act", "annual", "board", "chairman", "chief", "commission", "company", "corp",
    "corporation", "counsel", "director", "directors", "exchange", "executive", "general",
    "group", "holdings", "inc", "item", "llc", "ltd", "officer", "president", "registrant",
    "report", "secretary", "securities", "the", "treasurer", "vice",
}


# ------------------------------------------------------------------- name matching
def _norm_company_name(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    s = re.sub(_SUFFIX_WORDS, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _similarity(a: str, b: str) -> float:
    na, nb = _norm_company_name(a), _norm_company_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na.startswith(nb + " ") or nb.startswith(na + " "):
        return 0.9
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _parse_atom_filers(content: bytes) -> list[dict]:
    """(name, cik) pairs from a browse-edgar atom feed; bytes because the XML
    declaration says ISO-8859-1 and ET refuses str input carrying a declaration."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    filers: list[dict] = []
    for elem in root.iter():
        if elem.tag.split("}")[-1] != "company-info":
            continue
        name = cik = ""
        for child in elem.iter():
            tag = child.tag.split("}")[-1]
            if tag == "conformed-name":
                name = (child.text or "").strip()
            elif tag == "cik":
                cik = (child.text or "").strip()
        # Multi-match atom output omits conformed-name (SEC bug) -> unusable, skip.
        if name and cik.isdigit():
            filers.append({"name": name, "cik": cik.zfill(10)})
    return filers


# --------------------------------------------------------------- officer extraction
def _title_alt(role: str) -> str:
    phrases = [
        p for p in ROLE_TITLES.get(role, ())
        if len(p.split()) >= 2 and p not in _WEAK_PHRASES
    ]
    # re.escape turns spaces into "\ ", so swap that form (then any bare space) for \s+
    return "|".join(re.escape(p).replace("\\ ", r"\s+").replace(" ", r"\s+") for p in phrases)


@cache
def _role_regexes(role: str) -> tuple[re.Pattern | None, re.Pattern | None]:
    alt = _title_alt(role)
    if not alt:
        return None, None
    appt = re.compile(
        r"(?i:appointed|named|promoted|elected|hired)\s+(?:(?i:mr|ms|mrs|dr)\.\s*)?"
        r"(?P<name>" + _NAME_PART + r")"
        r"(?:\s*,\s*(?:(?i:age)\s+)?\d{2}\s*,?)?"
        r"\s+(?i:as|to\s+serve\s+as|to\s+the\s+position\s+of|to\s+be)\s+"
        r"(?:(?i:its|our|the|company['’]s|a|an)\s+)*"
        r"(?:(?i:new|interim|acting|executive|senior)\s+)*"
        r"(?:(?i:vice\s+president)[\s,\-]+(?:(?i:and)\s+)?)?"
        r"(?P<title>(?i:" + alt + r"))"
    )
    sig = re.compile(
        r"/s/\s*(?P<name>" + _NAME_PART + r")"
        r"[\s,]+(?:(?P=name)[\s,]+)?"
        r"(?:[A-Z][A-Za-z&'’.,\- ]{0,60}?[\s,\-])??"
        r"(?P<title>(?i:" + alt + r"))"
    )
    return appt, sig


def _plausible_name(name: str) -> bool:
    toks = name.split()
    if not (2 <= len(toks) <= 3):
        return False
    return all(t.lower().strip(".,'’") not in _NAME_STOP_WORDS for t in toks)


def _extract_people(text: str, role: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for rx in _role_regexes(role):
        if rx is None:
            continue
        for m in rx.finditer(text):
            name = re.sub(r"\s+", " ", m.group("name")).strip(" ,")
            if name.isupper():
                name = name.title()
            title = re.sub(r"\s+", " ", m.group("title")).strip()
            if _plausible_name(name) and title_matches_role(title, role):
                found.append((name, title))
    return found


# --------------------------------------------------------------------------- provider
@register
class Edgar(ProviderBase):
    name = "edgar"
    key_env = None
    is_free = True
    rps = 5.0

    def _unit_cost(self) -> float:
        raw = self.ctx.settings.key(COST_ENV)
        if raw is None:
            return COST_PER_CALL_USD
        try:
            return float(raw)
        except ValueError:
            raise ProviderError(f"{COST_ENV} must be a number, got {raw!r}") from None

    # ------------------------------------------------------------- fetching
    async def _browse(self, name: str) -> list[dict]:
        params = {
            "action": "getcompany", "company": name, "type": "10-K",
            "owner": "include", "count": "10", "output": "atom",
        }

        async def fetch():
            resp = await net.fetch(self.ctx.http, "GET", BROWSE_URL, headers=dict(_HEADERS), params=params)
            return {"filers": _parse_atom_filers(resp.content)}

        data = await self.cached_json(
            "browse", {"company": name.lower(), "type": "10-K"}, fetch,
            cost_usd=self._unit_cost(), credits=1.0,
        )
        return data.get("filers", [])

    async def _filer(self, company: Company) -> dict | None:
        candidates: list[str] = []
        for n in (company.entity_name, company.name, company.parent_company):
            n = (n or "").strip()
            if n and n.lower() not in (c.lower() for c in candidates):
                candidates.append(n)
        for cand in candidates:
            best, best_score = None, 0.0
            for filer in await self._browse(cand):
                score = _similarity(filer.get("name", ""), cand)
                if score > best_score:
                    best, best_score = filer, score
            if best and best_score >= MATCH_THRESHOLD:
                return best
        return None

    async def _fts(self, cik: str, phrase: str) -> list[dict]:
        params = {"q": f'"{phrase}"', "ciks": cik, "forms": "8-K"}

        async def fetch():
            data = await net.fetch_json(self.ctx.http, "GET", FTS_URL, headers=dict(_HEADERS), params=params)
            out = []
            for h in ((data.get("hits") or {}).get("hits") or [])[:10]:
                src = h.get("_source") or {}
                out.append({
                    "id": h.get("_id", ""), "adsh": src.get("adsh", ""),
                    "date": src.get("file_date", ""), "items": src.get("items") or [],
                    "form": src.get("form", ""),
                })
            return {"hits": out}

        data = await self.cached_json(
            "fts", {"cik": cik, "phrase": phrase, "forms": "8-K"}, fetch,
            cost_usd=self._unit_cost(), credits=1.0,
        )
        return data.get("hits", [])

    async def _doc(self, cik: str, adsh: str, fname: str) -> dict:
        url = f"{ARCHIVES_BASE}/{int(cik)}/{adsh.replace('-', '')}/{fname}"

        async def fetch():
            resp = await net.fetch(self.ctx.http, "GET", url, headers=dict(_HEADERS))
            text = BeautifulSoup(resp.text[:1_500_000], "html.parser").get_text(" ")
            return {"url": url, "text": re.sub(r"\s+", " ", text)[:MAX_DOC_TEXT]}

        return await self.cached_json("doc", {"url": url}, fetch, cost_usd=self._unit_cost(), credits=1.0)

    # -------------------------------------------------------- capabilities
    async def company_info(self, company: Company) -> CompanyInfo | None:
        filer = await self._filer(company)
        if not filer:
            return None
        return CompanyInfo(
            entity_name=filer["name"],
            revenue_hint="SEC filer (public or public parent)",
            source=self.name,
            raw={"cik": filer["cik"]},
        )

    async def identify(self, company: Company, role: str) -> list[PersonHit]:
        if role not in ROLE_TITLES:
            return []
        filer = await self._filer(company)
        if not filer:
            return []  # fast path: no SEC filer (private company) -> skip full-text entirely
        phrase = ROLE_TITLES[role][0]
        hits = sorted(await self._fts(filer["cik"], phrase), key=lambda h: h.get("date", ""), reverse=True)
        hits.sort(key=lambda h: "5.02" not in (h.get("items") or []))  # officer-change 8-Ks first
        out: dict[str, PersonHit] = {}
        for h in hits[:MAX_DOCS_PER_ROLE]:
            id_adsh, _, fname = (h.get("id") or "").partition(":")
            adsh = h.get("adsh") or id_adsh
            if not adsh or not fname or not filer["cik"].isdigit():
                continue
            doc = await self._doc(filer["cik"], adsh, fname)
            for name, title in _extract_people(doc.get("text", ""), role):
                key = re.sub(r"[^a-z]", "", name.lower())
                if key and key not in out:
                    out[key] = PersonHit(
                        name=name, title=title, role=role, source=self.name,
                        url=doc.get("url", ""), confidence=0.6,
                        raw={"adsh": adsh, "form": h.get("form", ""), "cik": filer["cik"]},
                    )
        return list(out.values())

    async def doctor(self) -> str:
        params = {"q": '"annual report"', "forms": "10-K"}

        async def fetch():
            data = await net.fetch_json(self.ctx.http, "GET", FTS_URL, headers=dict(_HEADERS), params=params)
            total = ((data.get("hits") or {}).get("total") or {}).get("value") or 0
            return {"total": int(total)}

        data = await self.cached_json("doctor", {"probe": "fts-smoke"}, fetch, ttl_days=0)
        return f"ok - EDGAR full-text search reachable ({data['total']:,} 10-K docs match smoke phrase)"
