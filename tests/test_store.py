from enrich.models import Company, EmailRec, PersonRec
from enrich.store import Store


def test_company_roundtrip(store: Store):
    c = Company(key="manual:acme", name="Acme", domain="acme.com", catch_all=True)
    store.upsert_company(c)
    got = store.get_company("manual:acme")
    assert got.name == "Acme" and got.catch_all is True
    c.status = "enriched"
    store.upsert_company(c)
    assert store.get_company("manual:acme").status == "enriched"
    assert store.find_company_by_domain("acme.com").key == "manual:acme"


def test_cache_and_ledger(store: Store):
    store.cache_put("hunter", "finder", {"q": 1}, {"ok": True}, cost_usd=0.02, run_id="r1")
    assert store.cache_get("hunter", "finder", {"q": 1}, ttl_days=30) == {"ok": True}
    assert store.cache_get("hunter", "finder", {"q": 2}, ttl_days=30) is None
    assert store.run_spend("r1") == 0.02
    by = store.spend_by_provider("r1")
    assert by[0]["provider"] == "hunter" and by[0]["calls"] == 1


def test_people_emails_blocklist(store: Store):
    store.save_person(PersonRec(company_key="k", role="ceo", name="Jane Smith", title="CEO", confidence=0.9))
    store.save_person(PersonRec(company_key="k", role="ceo", name="Jane Smith", title="CEO", confidence=0.5))
    people = store.people_for("k", "ceo")
    assert len(people) == 1 and people[0].confidence == 0.9  # max() on conflict
    store.save_email(EmailRec(company_key="k", role="ceo", email="jane@x.com", tier="A"))
    store.save_email(EmailRec(company_key="k", role="ceo", email="jane@x.com", tier="B"))
    assert store.emails_for("k")[0].tier == "B"
    store.block_email("Bad@X.com", "bounced")
    assert store.is_blocked("bad@x.com")


def test_domain_pattern_keeps_higher_confidence(store: Store):
    store.save_domain_pattern("x.com", pattern="{first}", confidence=0.9, source="hunter", sample_size=10)
    store.save_domain_pattern("x.com", pattern="{f}{last}", confidence=0.4, source="sheet", sample_size=3)
    assert store.get_domain_pattern("x.com")["pattern"] == "{first}"
