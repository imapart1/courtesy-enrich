"""Domain models, role/title taxonomy, tier logic."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel, Field

ROLES = ["legal", "ceo", "coo", "cfo", "privacy"]

# Ordered: earlier = stronger match for the role. Matching is substring-on-normalized-title.
ROLE_TITLES: dict[str, list[str]] = {
    "legal": [
        "general counsel", "chief legal officer", "head of legal", "vp of legal", "vp legal",
        "vice president legal", "vice president, legal", "deputy general counsel",
        "associate general counsel", "director of legal", "legal director", "senior counsel",
        "corporate counsel", "legal counsel", "counsel",
    ],
    "ceo": [
        "chief executive officer", "ceo", "co-founder", "cofounder", "co founder", "founder",
        "president", "owner", "managing member", "managing partner", "managing director", "principal",
    ],
    "coo": [
        "chief operating officer", "coo", "vp of operations", "vp operations",
        "vice president of operations", "head of operations", "director of operations",
    ],
    "cfo": [
        "chief financial officer", "cfo", "vp of finance", "vp finance",
        "vice president of finance", "head of finance", "controller",
    ],
    "privacy": [
        "chief privacy officer", "data protection officer", "privacy officer", "dpo",
        "privacy counsel", "head of privacy", "director of privacy",
        "chief information security officer", "ciso", "chief compliance officer",
    ],
}

# checked against normalized titles (hyphens become spaces): "Ex-CEO" -> " ex ceo "
_DISQUALIFIERS = ("assistant to", "executive assistant", "office of", "former", " ex ", "advisor to")

GENERIC_LOCALS = {
    "privacy", "legal", "info", "support", "hello", "contact", "cs", "sales", "admin",
    "office", "team", "help", "careers", "hr", "press", "media", "customerservice",
    "service", "privacypolicy", "compliance", "billing", "orders", "marketing", "webmaster",
}

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z ]", " ", title.lower()).strip()


def title_matches_role(title: str, role: str) -> float:
    """0.0 = no match; 1.0 = strongest title for the role. Position in ROLE_TITLES decays score."""
    if not title:
        return 0.0
    t = " " + normalize_title(title) + " "
    if any(d in t for d in _DISQUALIFIERS):
        return 0.0
    phrases = ROLE_TITLES.get(role, [])
    for idx, phrase in enumerate(phrases):
        if f" {phrase} " in t or t.strip() == phrase:
            return max(0.7, 1.0 - 0.02 * idx)
    return 0.0


def best_role_for_title(title: str) -> tuple[str | None, float]:
    best: tuple[str | None, float] = (None, 0.0)
    for role in ROLES:
        s = title_matches_role(title, role)
        if s > best[1]:
            best = (role, s)
    return best


def is_generic_local(local: str) -> bool:
    return local.lower().strip() in GENERIC_LOCALS


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email.strip()))


class Company(BaseModel):
    key: str                      # trello card URL for sheet rows, "manual:<slug>" otherwise
    name: str = ""
    website: str = ""
    domain: str = ""              # registrable domain of the website
    email_domain: str = ""        # domain emails actually live on (may differ)
    entity_name: str = ""
    parent_company: str = ""
    pattern: str = ""             # best-known email pattern, e.g. "{first}.{last}"
    pattern_confidence: float = 0.0
    catch_all: bool | None = None
    size_flag: str = ""           # e.g. "~25 employees", "<$10M revenue"
    status: str = "pending"       # pending|enriched|partial|no_contacts|anomaly|error
    source: str = "manual"        # manual|sheet
    plaintiff: str = ""
    demand_date: str = ""
    notes: str = ""
    error: str = ""
    added_at: str = Field(default_factory=utcnow)
    enriched_at: str = ""


class PersonRec(BaseModel):
    company_key: str
    role: str
    name: str
    title: str = ""
    provider: str = ""
    url: str = ""                 # evidence URL
    confidence: float = 0.0
    created_at: str = Field(default_factory=utcnow)
    raw: dict = Field(default_factory=dict)  # provider payload (e.g. Apollo person id for reveals)


class EmailRec(BaseModel):
    company_key: str
    role: str
    person_name: str = ""
    email: str
    method: str = "found"         # found|pattern|constructed|generic
    provider: str = ""
    verify_status: str = "unverified"  # deliverable|catch_all|undeliverable|unknown|error|unverified
    verify_provider: str = ""
    tier: str = ""                # A|B|C|D|X ("" = discarded/not yet tiered)
    confidence: float = 0.0
    run_id: str = ""
    created_at: str = Field(default_factory=utcnow)


def assign_tier(
    *,
    method: str,
    verify_status: str,
    found_confidence: float,
    pattern_proven: bool,
) -> str | None:
    """Map verification outcome to a confidence tier. None = discard the candidate."""
    if verify_status == "undeliverable":
        return None
    if method == "generic":
        return "A" if verify_status == "deliverable" else "D"
    if verify_status == "deliverable":
        return "A"
    if verify_status == "catch_all":
        if pattern_proven or (method == "found" and found_confidence >= 0.7):
            return "B"
        return "C"
    # unknown / unverified / error
    if method == "found" and found_confidence >= 0.9:
        return "B"
    return "C"
