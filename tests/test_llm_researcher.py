"""llm_researcher provider — no network, no real subprocess.

The Anthropic SDK is faked by monkeypatching anthropic.Anthropic with a canned
client; the CLI transport is faked by monkeypatching asyncio.create_subprocess_exec.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from enrich.context import BudgetExceeded
from enrich.models import Company
from enrich.providers import llm_researcher as mod
from enrich.providers.llm_researcher import LlmResearcher

RESULT = {
    "people": [
        {"name": "Jane Smith", "title": "General Counsel", "role": "legal",
         "evidence_url": "https://acme.com/team", "confidence": 0.9},
        {"name": "Bob Lee", "title": "Chief Executive Officer", "role": "ceo",
         "evidence_url": "https://acme.com/about", "confidence": 0.95},
    ],
    "entity_name": "Acme Corp LLC",
    "parent_company": "",
    "email_domain_hint": "acme.com",
    "notes": "",
}


def usage(inp=1000, out=500, searches=3):
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        server_tool_use=SimpleNamespace(web_search_requests=searches),
    )


def text_response(payload, stop_reason="end_turn", u=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        stop_reason=stop_reason,
        usage=u if u is not None else usage(),
    )


class FakeClient:
    """messages.create only — client.beta raises AttributeError, so the provider
    falls back to the non-beta path (exactly like an SDK without the fallbacks kwargs)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class BetaClient(FakeClient):
    """SDK that DOES accept the server-side-fallback beta kwargs."""

    def __init__(self, responses):
        super().__init__(responses)
        self.beta_calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._beta_create))

    def _beta_create(self, *, betas, fallbacks, **kwargs):
        self.beta_calls.append({"betas": betas, "fallbacks": fallbacks, **kwargs})
        return self._responses.pop(0)


def install_fake(monkeypatch, client):
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: client)


@pytest.fixture()
def company():
    return Company(key="manual:acme", name="Acme", website="acme.com", domain="acme.com")


@pytest.fixture()
def api_ctx(ctx, monkeypatch):
    ctx.flags.free_only = False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return ctx


# ------------------------------------------------------------------ API path
async def test_identify_happy_path_one_call_serves_all_roles(api_ctx, company, monkeypatch):
    fake = FakeClient([text_response(RESULT)])
    install_fake(monkeypatch, fake)
    async with api_ctx as ctx:
        p = LlmResearcher(ctx)
        legal = await p.identify(company, "legal")
        ceo = await p.identify(company, "ceo")
        cfo = await p.identify(company, "cfo")

    # one paid research call served all three role lookups
    assert len(fake.calls) == 1
    assert [h.name for h in legal] == ["Jane Smith"]
    hit = legal[0]
    assert hit.role == "legal" and hit.source == "llm_researcher"
    assert hit.title == "General Counsel"
    assert hit.url == "https://acme.com/team"
    assert hit.confidence == pytest.approx(0.75)  # source quality 0.75 x title match 1.0
    assert [h.name for h in ceo] == ["Bob Lee"]
    assert cfo == []

    # request shape: opus-5 rules — no temperature, no thinking param
    kw = fake.calls[0]
    assert kw["model"] == "claude-opus-5"
    assert kw["max_tokens"] == 16000
    assert kw["tools"] == [{"type": "web_search_20260209", "name": "web_search", "max_uses": 6}]
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["output_config"]["format"]["schema"] == mod.RESULT_SCHEMA
    assert "temperature" not in kw and "thinking" not in kw
    assert kw["messages"][0]["role"] == "user"
    assert "Acme" in kw["messages"][0]["content"]

    # cost arithmetic recorded: 1000*5/1e6 + 500*25/1e6 + 3*0.01
    row = next(r for r in ctx.store.spend_by_provider(ctx.run_id) if r["provider"] == "llm_researcher")
    assert row["calls"] == 1
    assert row["usd"] == pytest.approx(0.0475)
    assert row["credits"] == 3


async def test_empty_people_returns_empty_list(api_ctx, company, monkeypatch):
    fake = FakeClient([text_response(dict(RESULT, people=[]))])
    install_fake(monkeypatch, fake)
    async with api_ctx as ctx:
        hits = await LlmResearcher(ctx).identify(company, "legal")
    assert hits == []


async def test_refusal_returns_empty_and_is_noted(api_ctx, company, monkeypatch):
    refusal = SimpleNamespace(content=[], stop_reason="refusal", usage=usage(inp=200, out=10, searches=0))
    fake = FakeClient([refusal])
    install_fake(monkeypatch, fake)
    async with api_ctx as ctx:
        hits = await LlmResearcher(ctx).identify(company, "legal")
    assert hits == []
    cached = ctx.store.cache_get("llm_researcher", "research", {"domain": "acme.com"},
                                 ctx.settings.cache_ttl_days)
    assert cached["refused"] is True
    assert "refus" in cached["notes"]
    # token cost of the refused turn is still real spend
    row = next(r for r in ctx.store.spend_by_provider(ctx.run_id) if r["provider"] == "llm_researcher")
    assert row["usd"] == pytest.approx(200 * 5 / 1e6 + 10 * 25 / 1e6)
    assert row["credits"] == 0


async def test_pause_turn_continuation(api_ctx, company, monkeypatch):
    paused = SimpleNamespace(
        content=[SimpleNamespace(type="server_tool_use", name="web_search")],
        stop_reason="pause_turn",
        usage=usage(inp=1000, out=100, searches=2),
    )
    final = text_response(RESULT, u=usage(inp=2000, out=400, searches=1))
    fake = FakeClient([paused, final])
    install_fake(monkeypatch, fake)
    async with api_ctx as ctx:
        hits = await LlmResearcher(ctx).identify(company, "ceo")

    assert [h.name for h in hits] == ["Bob Lee"]
    assert len(fake.calls) == 2
    # continuation re-sends the user message plus the paused assistant turn
    msgs = fake.calls[1]["messages"]
    assert msgs[0]["role"] == "user"
    assert msgs[1] == {"role": "assistant", "content": list(paused.content)}
    # cost sums both requests: (0.005+0.0025+0.02) + (0.01+0.01+0.01)
    row = next(r for r in ctx.store.spend_by_provider(ctx.run_id) if r["provider"] == "llm_researcher")
    assert row["usd"] == pytest.approx(0.0575)
    assert row["credits"] == 3


async def test_api_http_error_propagates_and_nothing_cached(api_ctx, company, monkeypatch):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(500, request=req, text="overloaded")
    err = anthropic.APIStatusError("server error", response=resp, body=None)
    fake = FakeClient([err])
    install_fake(monkeypatch, fake)
    async with api_ctx as ctx:
        with pytest.raises(anthropic.APIStatusError):
            await LlmResearcher(ctx).identify(company, "legal")
    assert ctx.store.cache_get("llm_researcher", "research", {"domain": "acme.com"},
                               ctx.settings.cache_ttl_days) is None
    assert ctx.store.spend_by_provider(ctx.run_id) == []


async def test_budget_checked_with_estimate_before_call(api_ctx, company, monkeypatch):
    fake = FakeClient([text_response(RESULT)])
    install_fake(monkeypatch, fake)
    api_ctx.budget.limit_usd = 0.10  # below the $0.30 default estimate
    async with api_ctx as ctx:
        with pytest.raises(BudgetExceeded):
            await LlmResearcher(ctx).identify(company, "legal")
    assert fake.calls == []  # never reached the SDK


async def test_cost_constants_env_override(api_ctx, company, monkeypatch):
    monkeypatch.setenv("LLM_COST_PER_MTOK_INPUT", "0")
    monkeypatch.setenv("LLM_COST_PER_MTOK_OUTPUT", "0")
    monkeypatch.setenv("LLM_COST_PER_WEB_SEARCH", "0")
    fake = FakeClient([text_response(RESULT)])
    install_fake(monkeypatch, fake)
    async with api_ctx as ctx:
        await LlmResearcher(ctx).identify(company, "legal")
    row = next(r for r in ctx.store.spend_by_provider(ctx.run_id) if r["provider"] == "llm_researcher")
    assert row["usd"] == 0.0


async def test_beta_fallbacks_used_when_sdk_accepts_kwargs(api_ctx, company, monkeypatch):
    fake = BetaClient([text_response(RESULT)])
    install_fake(monkeypatch, fake)
    async with api_ctx as ctx:
        hits = await LlmResearcher(ctx).identify(company, "legal")
    assert hits
    assert fake.calls == []  # non-beta path untouched
    assert fake.beta_calls[0]["betas"] == ["server-side-fallback-2026-07-01"]
    assert fake.beta_calls[0]["fallbacks"] == "default"


async def test_company_info_reads_only_from_cache(api_ctx, company, monkeypatch):
    fake = FakeClient([text_response(RESULT)])
    install_fake(monkeypatch, fake)
    async with api_ctx as ctx:
        p = LlmResearcher(ctx)
        assert await p.company_info(company) is None  # cache miss: no LLM call
        assert fake.calls == []
        await p.identify(company, "legal")
        info = await p.company_info(company)
    assert len(fake.calls) == 1
    assert info is not None
    assert info.entity_name == "Acme Corp LLC"
    assert info.email_domain == "acme.com"
    assert info.source == "llm_researcher"


# ------------------------------------------------------------------ CLI path
def _fake_cli(monkeypatch, stdout: bytes, returncode: int = 0):
    seen: dict = {}

    class FakeProc:
        def __init__(self):
            self.returncode = returncode

        async def communicate(self):
            return stdout, b""

    async def fake_exec(*argv, **kw):
        seen["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_exec)
    return seen


async def test_cli_transport_strips_json_fences(ctx, company, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ctx.flags.free_only = True  # CLI transport still counts as free
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/local/bin/claude")
    wrapped = "Sure! Here is what I found:\n```json\n" + json.dumps(RESULT) + "\n```\nLet me know."
    seen = _fake_cli(monkeypatch, json.dumps({"result": wrapped}).encode())

    async with ctx:
        p = LlmResearcher(ctx)
        assert p.available() == (True, "")
        assert p.is_free is True
        hits = await p.identify(company, "legal")

    assert [h.name for h in hits] == ["Jane Smith"]
    argv = seen["argv"]
    assert argv[0] == "claude" and argv[1] == "-p"
    assert ("--output-format", "json") == (argv[3], argv[4]) or "--output-format" in argv
    assert "--allowedTools" in argv and "WebSearch" in argv
    assert "--max-turns" in argv and "8" in argv
    cached = ctx.store.cache_get("llm_researcher", "research", {"domain": "acme.com"},
                                 ctx.settings.cache_ttl_days)
    assert cached["transport"] == "cli"
    row = next(r for r in ctx.store.spend_by_provider(ctx.run_id) if r["provider"] == "llm_researcher")
    assert row["usd"] == 0.0 and row["credits"] == 0


async def test_cli_failure_returns_empty_and_is_not_cached(ctx, company, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/local/bin/claude")
    _fake_cli(monkeypatch, b"", returncode=1)
    async with ctx:
        hits = await LlmResearcher(ctx).identify(company, "legal")
    assert hits == []
    # transient CLI failure must not be cached (next run retries)
    assert ctx.store.cache_get("llm_researcher", "research", {"domain": "acme.com"},
                               ctx.settings.cache_ttl_days) is None


# --------------------------------------------------------------- availability
def test_available_matrix(ctx, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    p = LlmResearcher(ctx)

    ctx.flags.free_only = True  # no key, no CLI, free-only
    ok, why = p.available()
    assert not ok and "free-only" in why

    ctx.flags.free_only = False  # no key, no CLI
    ok, why = p.available()
    assert not ok and "ANTHROPIC_API_KEY" in why

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")  # key, paid allowed
    assert p.available() == (True, "")
    assert p.is_free is False

    ctx.flags.free_only = True  # key but free-only and no CLI -> excluded
    ok, _ = p.available()
    assert not ok
