"""Output: enriched CSV (paste-ready) and optional Google Sheets push."""

from __future__ import annotations

import csv
from pathlib import Path

from .models import ROLES, EmailRec, PersonRec
from .store import Store

ROLE_COLS = ("name", "title", "email", "tier", "source")


def _pick_role_rows(people: list[PersonRec], emails: list[EmailRec], role: str) -> dict:
    """Best person + best email for one role."""
    out = dict.fromkeys((f"{role}_{c}" for c in ROLE_COLS), "")
    role_people = [p for p in people if p.role == role]
    if role_people:
        best = role_people[0]
        out[f"{role}_name"], out[f"{role}_title"], out[f"{role}_source"] = best.name, best.title, best.url or best.provider
    tier_order = {"A": 0, "B": 1, "C": 2, "D": 3}
    role_emails = sorted(
        (e for e in emails if e.role == role and e.tier in tier_order),
        key=lambda e: (tier_order[e.tier], -e.confidence),
    )
    if role_emails:
        best_e = role_emails[0]
        out[f"{role}_email"], out[f"{role}_tier"] = best_e.email, best_e.tier
        if not out[f"{role}_name"]:
            out[f"{role}_name"] = best_e.person_name
    return out


def paste_ready(emails: list[EmailRec], *, tiers: tuple[str, ...] = ("A", "B", "D"), cap: int = 8) -> str:
    """Comma-separated email cell matching the sheet's existing format."""
    tier_order = {t: i for i, t in enumerate(("A", "B", "C", "D"))}
    role_order = {r: i for i, r in enumerate(ROLES)}
    picked, seen = [], set()
    for e in sorted(
        (e for e in emails if e.tier in tiers),
        key=lambda e: (role_order.get(e.role, 9), tier_order.get(e.tier, 9), -e.confidence),
    ):
        if e.email not in seen:
            seen.add(e.email)
            picked.append(e.email)
        if len(picked) >= cap:
            break
    return ", ".join(picked)


def export_enriched_csv(store: Store, out_path: Path, keys: list[str] | None = None, cap: int = 8) -> int:
    companies = [c for c in store.companies() if (keys is None or c.key in keys)]
    companies = [c for c in companies if c.source == "manual" or c.status != "pending"]
    header = [
        "key", "defendant", "website", "email_domain", "pattern", "pattern_confidence",
        "catch_all", "size_flag", "status",
    ]
    for role in ROLES:
        header += [f"{role}_{c}" for c in ROLE_COLS]
    header += ["email_paste_ready", "review_needed", "notes", "enriched_at"]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for c in companies:
            people = store.people_for(c.key)
            emails = store.emails_for(c.key)
            if c.source == "sheet" and not emails and c.status == "enriched":
                continue  # rows completed by hand before the pipeline existed
            row = {
                "key": c.key, "defendant": c.name, "website": c.website,
                "email_domain": c.email_domain or c.domain, "pattern": c.pattern,
                "pattern_confidence": c.pattern_confidence or "",
                "catch_all": {True: "yes", False: "no"}.get(c.catch_all, ""),
                "size_flag": c.size_flag, "status": c.status,
                "email_paste_ready": paste_ready(emails, cap=cap),
                "review_needed": "yes" if any(e.tier == "C" for e in emails) or c.status == "anomaly" else "",
                "notes": c.notes, "enriched_at": c.enriched_at,
            }
            for role in ROLES:
                row.update(_pick_role_rows(people, emails, role))
            w.writerow([row.get(h, "") for h in header])
            n += 1
    return n


def push_gsheet(store: Store, sheet_id: str, service_account_file: str, worksheet: str = "Enriched") -> int:
    """Write the enriched table to a dedicated worksheet. Requires gsheets extra + shared sheet."""
    try:
        import gspread
    except ImportError as e:
        raise RuntimeError("gspread not installed - run: uv sync --extra gsheets") from e
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    export_enriched_csv(store, tmp_path)
    rows = list(csv.reader(open(tmp_path, encoding="utf-8")))
    gc = gspread.service_account(filename=service_account_file)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(worksheet)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(worksheet, rows=len(rows) + 10, cols=len(rows[0]) + 2)
    ws.update(rows, "A1")
    tmp_path.unlink(missing_ok=True)
    return len(rows) - 1
