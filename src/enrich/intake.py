"""Intake: freeform names/URLs (the easy path) and the Contact Research CSV export."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from .domains import looks_like_domain, normalize_domain
from .models import Company, is_generic_local, valid_email
from .patterns import learn_domain_shape
from .store import Store

# Contact-research CSV column indexes (see docs/data-audit.md)
COL_CARD, COL_PFIRST, COL_PLAST, COL_DEFENDANT, COL_EMAIL, COL_DEMAND, COL_WEBSITE, \
    COL_NOTES, COL_STATUS, COL_COMPLETED, COL_TRELLO = range(11)


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def parse_freeform(items: list[str]) -> list[Company]:
    """Each item is a company: 'Acme Corp', 'acme.com', 'https://acme.com',
    'Acme Corp | acme.com', or 'Acme Corp, acme.com' (name first)."""
    out: list[Company] = []
    for raw in items:
        raw = raw.strip().strip('"')
        if not raw or raw.startswith("#"):
            continue
        name, site = "", ""
        if "|" in raw:
            name, _, site = (p.strip() for p in raw.partition("|"))
        elif looks_like_domain(raw):
            site = raw
        elif "," in raw and looks_like_domain(raw.rsplit(",", 1)[1].strip()):
            name, site = raw.rsplit(",", 1)[0].strip(), raw.rsplit(",", 1)[1].strip()
        else:
            name = raw
        domain = normalize_domain(site) if site else ""
        if not name:
            name = domain.split(".")[0].replace("-", " ").title() if domain else raw
        key = f"manual:{slugify(domain or name)}"
        out.append(Company(key=key, name=name, website=site or domain, domain=domain, source="manual"))
    return out


def read_freeform_file(path: Path) -> list[Company]:
    return parse_freeform(path.read_text().splitlines())


@dataclass
class SheetImport:
    queued: list[Company] = field(default_factory=list)      # no emails yet -> work queue
    learned: list[Company] = field(default_factory=list)     # emails present -> training data
    bounced: list[tuple[Company, list[str]]] = field(default_factory=list)
    skipped_rows: int = 0
    known_email_count: int = 0


def _split_emails(cell: str) -> list[str]:
    return [e.strip().lower() for e in re.split(r"[,;\s]+", cell or "") if valid_email(e.strip())]


def load_sheet_csv(path: Path) -> SheetImport:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    imp = SheetImport()
    for r in rows:
        cell = lambda i, r=r: r[i].strip() if len(r) > i else ""  # noqa: E731
        card, defendant, website = cell(COL_CARD), cell(COL_DEFENDANT), cell(COL_WEBSITE)
        if not defendant or not card.startswith("http"):
            imp.skipped_rows += 1
            continue
        emails = _split_emails(cell(COL_EMAIL))
        status = cell(COL_STATUS)
        c = Company(
            key=card,
            name=defendant,
            website=website,
            domain=normalize_domain(website),
            source="sheet",
            plaintiff=f"{cell(COL_PFIRST)} {cell(COL_PLAST)}".strip(),
            demand_date=cell(COL_DEMAND),
            notes=cell(COL_NOTES),
            status="pending",
        )
        notes_l = c.notes.lower()
        if emails:
            imp.learned.append(c)
            imp.known_email_count += len(emails)
        elif status == "Emails Did Not Work" or "did not work" in notes_l:
            bad = _split_emails(c.notes)
            imp.bounced.append((c, bad))
        else:
            imp.queued.append(c)
        c.__dict__["_emails"] = emails  # transient, consumed by import_sheet
    return imp


def import_sheet(store: Store, imp: SheetImport) -> dict:
    """Queue pending rows; learn domains/patterns + ground truth from completed rows;
    blocklist bounced addresses and queue those rows for re-run."""
    stats = {"queued": 0, "learned_domains": 0, "known_emails": 0, "bounced_blocked": 0, "requeued_bounced": 0}

    for c in imp.queued:
        if not store.get_company(c.key):
            stats["queued"] += 1
        store.upsert_company(c)

    by_domain: dict[str, list[str]] = {}
    for c in imp.learned:
        emails = c.__dict__.get("_emails", [])
        # actual email domain can differ from the website domain — learn from the emails
        domains = {e.split("@")[1] for e in emails}
        email_domain = max(domains, key=lambda d: sum(e.endswith("@" + d) for e in emails)) if domains else ""
        c.email_domain = email_domain
        c.status = "enriched"  # already done by hand; not part of the work queue
        store.upsert_company(c)
        for e in emails:
            d = e.split("@")[1]
            store.save_known_email(d, e, c.key)
            by_domain.setdefault(d, []).append(e.split("@")[0])
            stats["known_emails"] += 1

    for domain, locals_ in by_domain.items():
        personal = [x for x in locals_ if not is_generic_local(x)]
        shape, conf, n = learn_domain_shape(personal)
        if n:
            # discount tiny samples: 1 email must never count as a "proven" pattern
            conf = conf * min(1.0, n / 4)
            store.save_domain_pattern(domain, shape=shape, confidence=round(conf, 2), source="sheet", sample_size=n)
            stats["learned_domains"] += 1

    for c, bad in imp.bounced:
        for e in bad:
            store.block_email(e, "sheet: Emails Did Not Work")
            stats["bounced_blocked"] += 1
        c.status = "pending"
        c.notes = (c.notes + " [requeued after bounce]").strip()
        store.upsert_company(c)
        stats["requeued_bounced"] += 1

    return stats
