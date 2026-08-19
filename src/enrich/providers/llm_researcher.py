"""llm_researcher adapter: Claude + web search as the last-resort identify provider.

One research call per company covers ALL roles (cache params are keyed by domain
only); `identify(company, role)` filters the cached result by role, so five roles
share a single paid call. `company_info` reads only from the cache and never
triggers a fresh LLM call.

Two transports, picked automatically:

A) Anthropic API (env ANTHROPIC_API_KEY, `anthropic` SDK). Paid: real cost is
   computed from the response usage (input/output tokens + web searches) and
   recorded in the ledger; the budget is checked BEFORE the call with a
   conservative estimate (env LLM_COST_ESTIMATE, default $0.30).
B) Claude Code CLI (`claude` on PATH, no key needed) — uses the operator's
   existing subscription, so cost_usd is 0 and the provider counts as FREE:
   `is_free` is a property that returns True when the CLI transport is active,
   which keeps this provider usable in --free-only runs and after a budget cap.

`cached_json` cannot split "estimate before / actual after", so this module
mirrors its cache -> budget -> rate-limit -> fetch -> ledger flow manually.

Cost constants (paid tier for claude-opus-5, per docs/service-research.md), each
overridable via env var:
    LLM_COST_PER_MTOK_INPUT   (default 5.00 USD / 1M input tokens)
    LLM_COST_PER_MTOK_OUTPUT  (default 25.00 USD / 1M output tokens)
    LLM_COST_PER_WEB_SEARCH   (default 0.01 USD / search = $10 / 1k)
    LLM_COST_ESTIMATE         (default 0.30 USD pre-call budget estimate)
Free-tier/subscription users can set these to 0.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
from typing import Any
from urllib.parse import urlparse

import anthropic

from ..models import ROLES, Company, title_matches_role
from .base import CompanyInfo, PersonHit, ProviderBase, ProviderError, register

# --- per-unit prices (paid tier; see module docstring for env overrides) -----
COST_PER_MTOK_INPUT = 5.0
COST_PER_MTOK_OUTPUT = 25.0
COST_PER_WEB_SEARCH = 0.01
COST_ESTIMATE_USD = 0.30

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_SEARCHES = 6
MAX_CONTINUATIONS = 3          # pause_turn resumes per research call
CLI_TIMEOUT_S = 300.0
SOURCE_QUALITY = 0.75          # LLM research < a page that literally lists the team

ROLE_LONG: dict[str, str] = {
    "legal": "General Counsel / Chief Legal Officer / head of legal",
    "ceo": "Chief Executive Officer (or founder / president / owner at small companies)",
    "coo": "Chief Operating Officer / head of operations",
    "cfo": "Chief Financial Officer / head of finance",
    "privacy": "Chief Privacy Officer / Data Protection Officer / privacy lead",
}

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "people": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "title": {"type": "string"},
                    "role": {"type": "string", "enum": list(ROLES)},
                    "evidence_url": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["name", "title", "role", "evidence_url", "confidence"],
                "additionalProperties": False,
            },
        },
        "entity_name": {"type": "string"},
        "parent_company": {"type": "string"},
        "email_domain_hint": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["people", "entity_name", "parent_company", "email_domain_hint", "notes"],
    "additionalProperties": False,
}

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


# ------------------------------------------------------------------ helpers
def _clamp01(v: Any) -> float:
    try:
        return min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def _empty_result(note: str) -> dict:
    return {
        "people": [],
        "entity_name": "",
        "parent_company": "",
        "email_domain_hint": "",
        "notes": note,
        "refused": False,
    }


def _normalize(parsed: dict) -> dict:
    """Coerce a model-produced result into the exact shape we cache."""
    people = []
    for p in parsed.get("people") or []:
        if not isinstance(p, dict):
            continue
        rec = {
            "name": str(p.get("name") or "").strip(),
            "title": str(p.get("title") or "").strip(),
            "role": str(p.get("role") or "").strip().lower(),
            "evidence_url": str(p.get("evidence_url") or "").strip(),
            "confidence": _clamp01(p.get("confidence", 0.0)),
        }
        if rec["name"]:
            people.append(rec)
    return {
        "people": people,
        "entity_name": str(parsed.get("entity_name") or "").strip(),
        "parent_company": str(parsed.get("parent_company") or "").strip(),
        "email_domain_hint": str(parsed.get("email_domain_hint") or "").strip(),
        "notes": str(parsed.get("notes") or "").strip(),
        "refused": False,
    }


def _extract_json_object(text: str) -> dict | None:
    """Pull a JSON object out of possibly prose-wrapped / fence-wrapped text."""
    text = text.strip()
    candidates: list[str] = []
    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            out = json.loads(cand)
        except ValueError:
            continue
        if isinstance(out, dict):
            return out
    return None


def _website_domain(url: str) -> str:
    if not url:
        return ""
    host = urlparse(url if "//" in url else f"https://{url}").netloc
    return host.split(":")[0].removeprefix("www.").lower()


@register
class LlmResearcher(ProviderBase):
    name = "llm_researcher"
    key_env = "ANTHROPIC_API_KEY"
    # is_free is a property below: True only when the CLI transport is active.
    rps = 0.5

    # --------------------------------------------------------- availability
    def _api_ok(self) -> bool:
        return bool(self.api_key) and not self.ctx.flags.free_only

    @staticmethod
    def _cli_ok() -> bool:
        return shutil.which("claude") is not None

    def _transport(self) -> str | None:
        if self._api_ok():
            return "api"
        if self._cli_ok():
            return "cli"
        return None

    @property  # type: ignore[override]
    def is_free(self) -> bool:
        """The CLI transport rides the operator's subscription: no marginal cost."""
        return self._transport() == "cli"

    def available(self) -> tuple[bool, str]:
        if self._transport() is not None:
            return True, ""
        if self.ctx.flags.free_only:
            return False, "--free-only run and no claude CLI on PATH (CLI transport would be free)"
        return False, "no ANTHROPIC_API_KEY in .env and no claude CLI on PATH"

    # ------------------------------------------------------------ env costs
    def _envf(self, env_name: str, default: float) -> float:
        raw = self.ctx.settings.key(env_name)
        try:
            return float(raw) if raw is not None else default
        except ValueError:
            return default

    def _usage_cost(self, usage: Any) -> tuple[float, int]:
        """(usd, web_search_count) for one API response."""
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        stu = getattr(usage, "server_tool_use", None)
        searches = int(getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        usd = (
            in_tok * self._envf("LLM_COST_PER_MTOK_INPUT", COST_PER_MTOK_INPUT) / 1e6
            + out_tok * self._envf("LLM_COST_PER_MTOK_OUTPUT", COST_PER_MTOK_OUTPUT) / 1e6
            + searches * self._envf("LLM_COST_PER_WEB_SEARCH", COST_PER_WEB_SEARCH)
        )
        return usd, searches

    # ------------------------------------------------------------ prompting
    def _cache_key(self, company: Company) -> str:
        return (
            company.email_domain
            or company.domain
            or _website_domain(company.website)
            or company.name.strip().lower()
        )

    def _build_prompt(self, company: Company, domain: str) -> str:
        roles = [r for r in self.ctx.settings.roles if r in ROLE_LONG] or list(ROLE_LONG)
        role_lines = "\n".join(f"- {r}: {ROLE_LONG[r]}" for r in roles)
        who = company.name or domain
        site = f" (website {domain})" if domain else ""
        return (
            f"Research the leadership of {who}{site}. For each of the following "
            f"roles, find the person who currently holds it:\n{role_lines}\n\n"
            "Rules:\n"
            "- Only report a person if you find explicit evidence (a page naming "
            "them with their title), and include that evidence URL for each person.\n"
            "- No guessing. If you cannot verify who holds a role, or nobody holds "
            "it, simply omit it — an empty people list is a valid answer.\n"
            "- Also report the company's legal entity name, its parent company (if "
            "any), and the domain its employee emails appear to use; use empty "
            "strings when unknown.\n"
            "- Set confidence between 0 and 1 based on how direct and current the "
            "evidence is."
        )

    # --------------------------------------------------------- API transport
    def _create(self, client: Any, base: dict, messages: list) -> Any:
        """messages.create, preferring server-side refusal fallbacks when the SDK has them."""
        try:
            return client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=messages,
                **base,
            )
        except (TypeError, AttributeError):
            # SDK (or fake in tests) without the beta kwargs — plain path.
            return client.messages.create(messages=messages, **base)

    @staticmethod
    def _first_text_json(resp: Any) -> dict | None:
        for block in getattr(resp, "content", None) or []:
            if getattr(block, "type", "") == "text":
                try:
                    out = json.loads(block.text)
                except (TypeError, ValueError):
                    return None
                return out if isinstance(out, dict) else None
        return None

    def _research_api(self, prompt: str) -> dict:
        """Sync SDK call (run via asyncio.to_thread). Returns the normalized result
        dict with transport/cost metadata. Never logs or stores the API key."""
        client = anthropic.Anthropic(api_key=self.api_key)
        cfg = self.ctx.settings.llm_cfg
        model = str(cfg.get("model", DEFAULT_MODEL))
        max_searches = int(cfg.get("max_searches", DEFAULT_MAX_SEARCHES))
        # claude-opus-5: thinking is ON by default (no thinking param), and
        # temperature was removed (400 if sent) — pass neither.
        base = {
            "model": model,
            "max_tokens": 16000,
            "tools": [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}],
            "output_config": {"format": {"type": "json_schema", "schema": RESULT_SCHEMA}},
        }
        messages: list[dict] = [{"role": "user", "content": prompt}]
        total_usd, total_searches = 0.0, 0

        resp = self._create(client, base, messages)
        usd, searches = self._usage_cost(getattr(resp, "usage", None))
        total_usd += usd
        total_searches += searches

        hops = 0
        while getattr(resp, "stop_reason", "") == "pause_turn" and hops < MAX_CONTINUATIONS:
            # Resume: append the paused assistant turn and re-request (no extra
            # user message — the server detects the trailing server_tool_use).
            messages = messages + [{"role": "assistant", "content": list(resp.content)}]
            resp = self._create(client, base, messages)
            usd, searches = self._usage_cost(getattr(resp, "usage", None))
            total_usd += usd
            total_searches += searches
            hops += 1

        if getattr(resp, "stop_reason", "") == "refusal":
            data = _empty_result("model refused the request")
            data["refused"] = True
        elif getattr(resp, "stop_reason", "") == "pause_turn":
            data = _empty_result(f"still paused after {MAX_CONTINUATIONS} continuations")
        else:
            parsed = self._first_text_json(resp)
            data = _normalize(parsed) if parsed is not None else _empty_result(
                "no parseable JSON in model response"
            )
        data.update(
            transport="api", model=model,
            cost_usd=round(total_usd, 6), searches=total_searches,
        )
        return data

    # --------------------------------------------------------- CLI transport
    async def _research_cli(self, prompt: str) -> dict | None:
        """Claude Code CLI transport (operator subscription; cost 0). Returns None
        on transient failure (timeout / nonzero exit / garbage output) so the
        failure is NOT cached and the next run retries."""
        cli_prompt = (
            prompt
            + "\n\nRespond with ONLY a single JSON object matching this JSON schema"
            + " (no prose, no markdown):\n"
            + json.dumps(RESULT_SCHEMA)
        )
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", cli_prompt,
            "--output-format", "json",
            "--allowedTools", "WebSearch",
            "--max-turns", "8",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, _err = await asyncio.wait_for(proc.communicate(), timeout=CLI_TIMEOUT_S)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return None
        if proc.returncode != 0:
            return None
        try:
            envelope = json.loads(out.decode("utf-8", "replace") or "{}")
        except ValueError:
            return None
        if not isinstance(envelope, dict) or envelope.get("is_error"):
            return None
        parsed = _extract_json_object(str(envelope.get("result") or ""))
        if parsed is None:
            return None
        data = _normalize(parsed)
        data.update(transport="cli", model="claude-cli", cost_usd=0.0, searches=0)
        return data

    # ------------------------------------------------------------- research
    async def _research(self, company: Company) -> dict | None:
        """One research call per company, mirrored on cached_json's flow:
        cache -> budget(estimate) -> rate limit -> fetch -> cache_put(actual)."""
        key = self._cache_key(company)
        if not key:
            return None
        params = {"domain": key}
        ttl = self.ctx.settings.cache_ttl_days
        cached = self.ctx.store.cache_get(self.name, "research", params, ttl)
        if cached is not None:
            return cached

        transport = self._transport()
        if transport is None:
            raise ProviderError(
                "llm_researcher has no transport: set ANTHROPIC_API_KEY or install the claude CLI"
            )
        prompt = self._build_prompt(company, key if "." in key else "")
        assert self.ctx.budget is not None
        if transport == "api":
            self.ctx.budget.check(self._envf("LLM_COST_ESTIMATE", COST_ESTIMATE_USD))
            await self.ctx.limiter.acquire(self.name, self.rps)
            data = await asyncio.to_thread(self._research_api, prompt)
        else:
            await self.ctx.limiter.acquire(self.name, self.rps)
            data = await self._research_cli(prompt)
        if data is None:
            return None
        self.ctx.store.cache_put(
            self.name, "research", params, data,
            cost_usd=float(data.get("cost_usd", 0.0)),
            credits=float(data.get("searches", 0)),
            run_id=self.ctx.run_id,
        )
        return data

    # ----------------------------------------------------------- capabilities
    async def identify(self, company: Company, role: str) -> list[PersonHit]:
        data = await self._research(company)
        if not data:
            return []
        hits: list[PersonHit] = []
        for p in data.get("people") or []:
            title = str(p.get("title") or "")
            tm = title_matches_role(title, role)
            if tm <= 0.0:
                continue
            url = str(p.get("evidence_url") or "").strip()
            if not url:
                continue  # the prompt demands evidence; no URL = not trustworthy
            conf = SOURCE_QUALITY * tm
            llm_conf = _clamp01(p.get("confidence", 0.0))
            if llm_conf > 0.0:
                conf = min(conf, llm_conf)
            hits.append(
                PersonHit(
                    name=str(p.get("name") or ""),
                    title=title,
                    role=role,
                    source=self.name,
                    url=url,
                    confidence=round(conf, 2),
                    raw=dict(p),
                )
            )
        hits.sort(key=lambda h: h.confidence, reverse=True)
        return hits

    async def company_info(self, company: Company) -> CompanyInfo | None:
        """Cache-only read of a prior research call — never triggers a fresh one."""
        key = self._cache_key(company)
        if not key:
            return None
        cached = self.ctx.store.cache_get(
            self.name, "research", {"domain": key}, self.ctx.settings.cache_ttl_days
        )
        if not cached:
            return None
        if not (cached.get("entity_name") or cached.get("parent_company") or cached.get("email_domain_hint")):
            return None
        return CompanyInfo(
            entity_name=str(cached.get("entity_name") or ""),
            parent_company=str(cached.get("parent_company") or ""),
            email_domain=str(cached.get("email_domain_hint") or ""),
            source=self.name,
            raw=dict(cached),
        )

    async def doctor(self) -> str:
        """Report the active transport with the cheapest possible probe."""
        if self._api_ok():
            model = str(self.ctx.settings.llm_cfg.get("model", DEFAULT_MODEL))

            def _probe() -> str:
                client = anthropic.Anthropic(api_key=self.api_key)
                m = client.models.retrieve(model)  # free metadata endpoint
                return str(getattr(m, "id", model))

            try:
                mid = await asyncio.to_thread(_probe)
                return f"ok - api transport, model {mid} reachable"
            except Exception as e:  # noqa: BLE001 - doctor reports, never raises
                return f"error - api transport: {type(e).__name__}: {e}"
        if self._cli_ok():
            version = ""
            with contextlib.suppress(Exception):
                proc = await asyncio.create_subprocess_exec(
                    "claude", "--version",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
                if proc.returncode == 0:
                    version = out.decode("utf-8", "replace").strip().splitlines()[0]
            return f"ok - cli transport ({version or 'claude on PATH'}), cost $0"
        return "unavailable - set ANTHROPIC_API_KEY or install the claude CLI"
