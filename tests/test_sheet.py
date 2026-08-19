"""sheet pseudo-provider: answers from what `enrich pull` already learned. No network."""

from enrich.models import Company
from enrich.providers.sheet import Sheet


def company(**kw) -> Company:
    return Company(key="manual:acme", name="Acme", domain="acme.com", email_domain="acme.com", **kw)


def provider(ctx) -> Sheet:
    return Sheet(ctx)


async def test_domain_pattern_proven_passthrough(ctx):
    # a genuinely proven pattern comes back verbatim, confidence untouched
    ctx.store.save_domain_pattern(
        "acme.com", pattern="{first}.{last}", confidence=0.9, source="hunter", sample_size=12
    )
    hit = await provider(ctx).domain_pattern(company())
    assert hit is not None
    assert hit.pattern == "{first}.{last}"
    assert hit.confidence == 0.9
    assert hit.source == "sheet:hunter"
    assert hit.sample_size == 12


async def test_domain_pattern_none_when_unknown(ctx):
    assert await provider(ctx).domain_pattern(company()) is None


async def test_shape_only_hit_is_not_a_proven_pattern(ctx):
    """Regression (Fix B): a shape-only sheet record must surface pattern='' with the
    shape's confidence UNCHANGED — never inflated into a concrete '{first}.{last}' guess.

    Coordinated with pipeline._pattern_proven, which only reads confidence as proof when
    c.pattern is non-empty; here the empty pattern keeps a shape-only hit from ever
    seeding a contradicting tier-B guess."""
    ctx.store.save_domain_pattern(
        "acme.com", shape="first.last", confidence=0.55, source="sheet-shape", sample_size=7
    )
    hit = await provider(ctx).domain_pattern(company())
    assert hit is not None
    assert hit.pattern == ""  # shape-only: no proven pattern is asserted
    assert hit.confidence == 0.55  # passed through, NOT inflated
    assert hit.sample_size == 7
    # the shape itself is still reachable for permute via the store, untouched
    assert ctx.store.get_domain_pattern("acme.com")["shape"] == "first.last"
