from enrich.config import find_root
from enrich.intake import import_sheet, load_sheet_csv, parse_freeform

SHEET = find_root() / "tests/fixtures/sample-sheet.csv"


def test_parse_freeform_variants():
    got = parse_freeform([
        "Acme Corp",
        "acme.com",
        "https://www.ExampleBrand.com/pages/contact",
        "Acme Corp | acme.com",
        "Beta Inc, beta.io",
        "",
        "# comment",
    ])
    assert len(got) == 5
    assert got[0].name == "Acme Corp" and got[0].domain == ""
    assert got[1].domain == "acme.com"
    assert got[2].domain == "examplebrand.com"
    assert got[3].name == "Acme Corp" and got[3].domain == "acme.com"
    assert got[4].name == "Beta Inc" and got[4].domain == "beta.io"
    assert all(c.key.startswith("manual:") for c in got)


def test_load_sample_sheet_csv():
    assert SHEET.exists(), "sample sheet fixture missing"
    imp = load_sheet_csv(SHEET)
    assert len(imp.queued) == 1
    assert len(imp.learned) == 2
    assert len(imp.bounced) == 1
    assert imp.known_email_count == 6


def test_import_sheet_learns_patterns(store):
    imp = load_sheet_csv(SHEET)
    stats = import_sheet(store, imp)
    assert stats["queued"] == 1
    assert stats["learned_domains"] == 2
    assert stats["bounced_blocked"] == 1
    rec = store.get_domain_pattern("acme-demo.example")
    assert rec and rec["shape"] in ("single_short", "single_long")
    c = store.get_company("https://example.com/c/brand")
    assert c is not None and c.email_domain == "parentco.example"
    assert store.is_blocked("stale@bounce.example")
