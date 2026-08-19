"""Rich console reporting over the store."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from .models import ROLES
from .sheet_io import paste_ready
from .store import Store


def summary_table(store: Store) -> Table:
    s = store.stats()
    t = Table(title="Queue status")
    t.add_column("status")
    t.add_column("companies", justify="right")
    for k in ("pending", "enriched", "partial", "no_contacts", "anomaly", "error"):
        t.add_row(k, str(s.get(k, 0)))
    return t


def tier_table(store: Store) -> Table:
    s = store.stats()
    t = Table(title="Emails by confidence tier")
    t.add_column("tier")
    t.add_column("meaning")
    t.add_column("count", justify="right")
    meanings = {
        "A": "verified deliverable", "B": "strong guess (proven pattern on catch-all)",
        "C": "weak guess - review", "D": "generic backstop", "X": "bounced",
    }
    for tier in ("A", "B", "C", "D", "X"):
        t.add_row(tier, meanings[tier], str(s["emails_by_tier"].get(tier, 0)))
    return t


def spend_table(store: Store, run_id: str | None = None) -> Table:
    t = Table(title=f"Provider spend{' (this run)' if run_id else ' (all time)'}")
    t.add_column("provider")
    t.add_column("calls", justify="right")
    t.add_column("credits", justify="right")
    t.add_column("USD", justify="right")
    for row in store.spend_by_provider(run_id):
        t.add_row(row["provider"], str(row["calls"]), f"{row['credits'] or 0:.1f}", f"${row['usd'] or 0:.2f}")
    return t


def company_table(store: Store, keys: list[str]) -> Table:
    t = Table(title="Results")
    t.add_column("defendant", max_width=28)
    t.add_column("status")
    for role in ROLES:
        t.add_column(role, max_width=30)
    t.add_column("paste-ready", max_width=40)
    for key in keys:
        c = store.get_company(key)
        if not c:
            continue
        emails = store.emails_for(key)
        people = {p.role: p for p in store.people_for(key)}
        cells = []
        for role in ROLES:
            best = next((e for e in sorted(emails, key=lambda e: e.tier or "Z") if e.role == role and e.tier), None)
            person = people.get(role)
            bits = []
            if person:
                bits.append(person.name)
            if best:
                bits.append(f"{best.email} [{best.tier}]")
            cells.append("\n".join(bits))
        t.add_row(c.name, c.status, *cells, paste_ready(emails))
    return t


def print_full_report(console: Console, store: Store, run_id: str | None = None) -> None:
    console.print(summary_table(store))
    console.print(tier_table(store))
    console.print(spend_table(store, run_id))
