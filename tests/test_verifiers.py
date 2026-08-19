"""MillionVerifier + Reoon adapters — respx-mocked HTTP, no network."""

from __future__ import annotations

import httpx
import pytest
import respx

from enrich.net import HttpError
from enrich.providers.base import ProviderError, VerifyResult
from enrich.providers.verify_millionverifier import CREDITS_URL, MillionVerifier
from enrich.providers.verify_millionverifier import VERIFY_URL as MV_URL
from enrich.providers.verify_reoon import BALANCE_URL, Reoon
from enrich.providers.verify_reoon import VERIFY_URL as REOON_URL

EMAIL = "jane@acme.com"


@pytest.fixture()
def mv_ctx(ctx, monkeypatch):
    ctx.flags.free_only = False
    monkeypatch.setenv("MILLIONVERIFIER_API_KEY", "test-key")
    monkeypatch.delenv("MILLIONVERIFIER_COST_PER_CREDIT", raising=False)
    return ctx


@pytest.fixture()
def reoon_ctx(ctx, monkeypatch):
    monkeypatch.setenv("REOON_API_KEY", "test-key")
    monkeypatch.delenv("REOON_COST_PER_CREDIT", raising=False)
    return ctx  # free_only stays True: reoon is is_free


def _spend(ctx, provider):
    rows = [r for r in ctx.store.spend_by_provider(ctx.run_id) if r["provider"] == provider]
    return rows[0] if rows else None


# ---------------------------------------------------------------- millionverifier

@respx.mock
async def test_mv_ok_parses_and_ledgers(mv_ctx):
    route = respx.get(MV_URL).mock(
        return_value=httpx.Response(200, json={"email": EMAIL, "result": "ok", "quality": "good", "credits": 99})
    )
    async with mv_ctx as ctx:
        p = MillionVerifier(ctx)
        assert p.available() == (True, "")
        res = await p.verify(EMAIL)
    assert isinstance(res, VerifyResult)
    assert res.status == "deliverable" and res.source == "millionverifier"
    assert res.raw["quality"] == "good"
    # request carried key/email as query params, never logged in cache params
    req_url = str(route.calls[0].request.url)
    assert "api=test-key" in req_url and f"email={EMAIL.replace('@', '%40')}" in req_url
    row = _spend(mv_ctx, "millionverifier")
    assert row and row["calls"] == 1 and row["credits"] == 1
    assert row["usd"] == pytest.approx(0.0039)


@respx.mock
@pytest.mark.parametrize(
    "result,expected",
    [
        ("invalid", "undeliverable"),
        ("disposable", "undeliverable"),
        ("catch_all", "catch_all"),
        ("unknown", "unknown"),
        ("unverified", "unknown"),
        ("", "unknown"),
    ],
)
async def test_mv_result_mapping(mv_ctx, result, expected):
    respx.get(MV_URL).mock(return_value=httpx.Response(200, json={"email": EMAIL, "result": result}))
    async with mv_ctx as ctx:
        res = await MillionVerifier(ctx).verify(EMAIL)
    assert res.status == expected


@respx.mock
async def test_mv_cache_hit_skips_http_and_double_spend(mv_ctx):
    route = respx.get(MV_URL).mock(return_value=httpx.Response(200, json={"result": "ok"}))
    async with mv_ctx as ctx:
        p = MillionVerifier(ctx)
        await p.verify(EMAIL)
        res2 = await p.verify(EMAIL)
    assert route.call_count == 1
    assert res2.status == "deliverable"
    row = _spend(mv_ctx, "millionverifier")
    assert row["calls"] == 1 and row["usd"] == pytest.approx(0.0039)


@respx.mock
async def test_mv_http_4xx_raises(mv_ctx):
    respx.get(MV_URL).mock(return_value=httpx.Response(400, text="bad request"))
    async with mv_ctx as ctx:
        with pytest.raises(HttpError) as ei:
            await MillionVerifier(ctx).verify(EMAIL)
    assert ei.value.status == 400
    assert _spend(mv_ctx, "millionverifier") is None  # failures never cached/ledgered


@respx.mock
async def test_mv_5xx_retries_then_raises(mv_ctx, monkeypatch):
    async def no_sleep(_):
        return None
    import enrich.net as net_mod
    monkeypatch.setattr(net_mod.asyncio, "sleep", no_sleep)
    route = respx.get(MV_URL).mock(return_value=httpx.Response(503, text="down"))
    async with mv_ctx as ctx:
        with pytest.raises(HttpError) as ei:
            await MillionVerifier(ctx).verify(EMAIL)
    assert ei.value.status == 503
    assert route.call_count == 3


@respx.mock
async def test_mv_error_payload_is_misconfiguration(mv_ctx):
    respx.get(MV_URL).mock(return_value=httpx.Response(200, json={"error": "api key is disabled", "result": ""}))
    async with mv_ctx as ctx:
        with pytest.raises(ProviderError, match="disabled"):
            await MillionVerifier(ctx).verify(EMAIL)
    assert _spend(mv_ctx, "millionverifier") is None


async def test_mv_available_needs_key_and_paid_run(ctx, monkeypatch):
    monkeypatch.delenv("MILLIONVERIFIER_API_KEY", raising=False)
    ctx.flags.free_only = False
    ok, why = MillionVerifier(ctx).available()
    assert not ok and "MILLIONVERIFIER_API_KEY" in why
    monkeypatch.setenv("MILLIONVERIFIER_API_KEY", "k")
    ctx.flags.free_only = True
    ok, why = MillionVerifier(ctx).available()
    assert not ok and "free-only" in why


@respx.mock
async def test_mv_cost_env_override(mv_ctx, monkeypatch):
    monkeypatch.setenv("MILLIONVERIFIER_COST_PER_CREDIT", "0")
    respx.get(MV_URL).mock(return_value=httpx.Response(200, json={"result": "ok"}))
    async with mv_ctx as ctx:
        await MillionVerifier(ctx).verify(EMAIL)
    row = _spend(mv_ctx, "millionverifier")
    assert row["usd"] == 0.0 and row["credits"] == 1


@respx.mock
async def test_mv_doctor(mv_ctx):
    respx.get(CREDITS_URL).mock(return_value=httpx.Response(200, json={"credits": 1850}))
    async with mv_ctx as ctx:
        out = await MillionVerifier(ctx).doctor()
    assert out == "ok - 1,850 credits left"


# ------------------------------------------------------------------------ reoon

@respx.mock
async def test_reoon_safe_parses_and_ledgers_free(reoon_ctx):
    route = respx.get(REOON_URL).mock(
        return_value=httpx.Response(
            200, json={"email": EMAIL, "status": "safe", "overall_score": 98, "verification_mode": "power"}
        )
    )
    async with reoon_ctx as ctx:
        p = Reoon(ctx)
        assert p.available() == (True, "")  # is_free: usable even on free_only runs
        res = await p.verify(EMAIL)
    assert res.status == "deliverable" and res.source == "reoon"
    assert res.raw["overall_score"] == 98
    assert "mode=power" in str(route.calls[0].request.url)
    row = _spend(reoon_ctx, "reoon")
    assert row and row["calls"] == 1 and row["credits"] == 1 and row["usd"] == 0.0


@respx.mock
@pytest.mark.parametrize(
    "status,expected",
    [
        ("valid", "deliverable"),
        ("invalid", "undeliverable"),
        ("disabled", "undeliverable"),
        ("catch_all", "catch_all"),
        ("accept_all", "catch_all"),
        ("disposable", "unknown"),
        ("inbox_full", "unknown"),
        ("role_account", "unknown"),
        ("spamtrap", "unknown"),
        ("unknown", "unknown"),
        ("something_new", "unknown"),
    ],
)
async def test_reoon_status_mapping(reoon_ctx, status, expected):
    respx.get(REOON_URL).mock(return_value=httpx.Response(200, json={"email": EMAIL, "status": status}))
    async with reoon_ctx as ctx:
        res = await Reoon(ctx).verify(EMAIL)
    assert res.status == expected


@respx.mock
async def test_reoon_http_4xx_raises(reoon_ctx):
    respx.get(REOON_URL).mock(return_value=httpx.Response(403, text="forbidden"))
    async with reoon_ctx as ctx:
        with pytest.raises(HttpError) as ei:
            await Reoon(ctx).verify(EMAIL)
    assert ei.value.status == 403
    assert _spend(reoon_ctx, "reoon") is None


@respx.mock
async def test_reoon_error_payload_is_misconfiguration(reoon_ctx):
    respx.get(REOON_URL).mock(return_value=httpx.Response(200, json={"status": "error", "reason": "invalid key"}))
    async with reoon_ctx as ctx:
        with pytest.raises(ProviderError, match="invalid key"):
            await Reoon(ctx).verify(EMAIL)


async def test_reoon_available_without_key(ctx, monkeypatch):
    monkeypatch.delenv("REOON_API_KEY", raising=False)
    ok, why = Reoon(ctx).available()
    assert not ok and "REOON_API_KEY" in why


@respx.mock
async def test_reoon_doctor(reoon_ctx):
    respx.get(BALANCE_URL).mock(
        return_value=httpx.Response(
            200,
            json={"api_status": "active", "remaining_daily_credits": 150,
                  "remaining_instant_credits": 5000, "status": "success"},
        )
    )
    async with reoon_ctx as ctx:
        out = await Reoon(ctx).doctor()
    assert out == "ok - 150 daily + 5,000 instant credits left"
