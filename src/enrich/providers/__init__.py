"""Import all provider modules so REGISTRY is populated. A broken adapter must not
kill the CLI — record the import error and move on."""

from __future__ import annotations

import importlib
import logging

log = logging.getLogger(__name__)

_MODULES = [
    "sheet", "permute", "sitescrape", "serp", "newsrss", "edgar",
    "hunter", "apollo", "anymail", "exa",
    "verify_millionverifier", "verify_reoon", "llm_researcher",
]

IMPORT_ERRORS: dict[str, str] = {}

for _m in _MODULES:
    try:
        importlib.import_module(f".{_m}", __name__)
    except Exception as e:  # noqa: BLE001 - isolate provider failures
        IMPORT_ERRORS[_m] = f"{type(e).__name__}: {e}"
        log.warning("provider module %s failed to import: %s", _m, e)

from .base import REGISTRY, get_providers  # noqa: E402,F401
