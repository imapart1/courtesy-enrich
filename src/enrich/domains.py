"""Domain utilities: normalization, MX lookup (free stage-1 signals)."""

from __future__ import annotations

import asyncio
import re

import tldextract

_extract = tldextract.TLDExtract(suffix_list_urls=())  # offline PSL snapshot


def normalize_domain(website_or_domain: str) -> str:
    """'https://www.UrbanSkinRx.com/pages/x' -> 'urbanskinrx.com'."""
    s = (website_or_domain or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"^[a-z]+://", "", s)
    s = s.split("/")[0].split("?")[0].split("@")[-1].split(":")[0]
    ext = _extract(s)
    if not ext.suffix:
        return ""
    return f"{ext.domain}.{ext.suffix}"


def looks_like_domain(s: str) -> bool:
    s = s.strip()
    return bool(re.match(r"^([a-z]+://)?[\w.-]+\.[a-z]{2,}(/.*)?$", s, re.I)) and " " not in s


async def mx_domain(domain: str) -> tuple[bool, str]:
    """(has_mx, mail_host). Flags 'Gmail-only' style domains via google MX host names."""
    try:
        import dns.asyncresolver

        answers = await dns.asyncresolver.resolve(domain, "MX")
        hosts = sorted(str(r.exchange).rstrip(".").lower() for r in answers)
        return (True, hosts[0] if hosts else "")
    except Exception:
        return (False, "")


async def mx_domains(domains: list[str], concurrency: int = 20) -> dict[str, tuple[bool, str]]:
    sem = asyncio.Semaphore(concurrency)

    async def one(d: str):
        async with sem:
            return d, await mx_domain(d)

    return dict(await asyncio.gather(*(one(d) for d in domains)))
