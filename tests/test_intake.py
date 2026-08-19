from pathlib import Path

from enrich.intake import import_sheet, load_sheet_csv, parse_freeform

SHEET = Path("data/input/contact-research-2026-08-12.csv")


def test_parse_freeform_variants():
    got = parse_freeform([
        "Blissy LLC",
        "paw.com",
        "https://www.UrbanSkinRx.com/pages/contact",
        "Acme Corp | acme.com",
        "Beta Inc, beta.io",
        "",
        "# comment",
    ])
    assert len(got) == 5
    assert got[0].name == "Blissy LLC" and got[0].domain == ""
    assert got[1].domain == "paw.com"
    assert got[2].domain == "urbanskinrx.com"
    assert got[3].name == "Acme Corp" and got[3].domain == "acme.com"
    assert got[4].name == "Beta Inc" and got[4].domain == "beta.io"
    assert all(c.key.startswith("manual:") for c in got)


def test_load_real_sheet_csv():
    assert SHEET.exists(), "sheet snapshot missing"
    imp = load_sheet_csv(SHEET)
    # 3 rows have email cells with no parseable address -> they belong in the queue
    assert len(imp.queued) == 263
    assert len(imp.learned) == 284
    assert len(imp.bounced) == 3
    assert imp.known_email_count > 1100


def test_import_sheet_learns_patterns(store):
    imp = load_sheet_csv(SHEET)
    stats = import_sheet(store, imp)
    assert stats["queued"] == 263
    assert stats["learned_domains"] > 200
    assert stats["bounced_blocked"] > 0
    # spot-check: blissy learned as first-name shape
    rec = store.get_domain_pattern("blissy.com")
    assert rec and rec["shape"] in ("single_short", "single_long")
    # USRx: email domain differs from website domain and was learned from emails
    c = store.get_company("https://trello.com/c/a9Q7UVj8/9161-usrx-llc-fcb-b-gibbons")
    assert c is not None and c.email_domain == "axnygroup.com"
    # bounced address is blocked
    assert store.is_blocked("gene@teslarati.com")
