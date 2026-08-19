from __future__ import annotations

from pathlib import Path

import pytest

from enrich.config import Settings
from enrich.context import Ctx, Flags
from enrich.store import Store


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "test.sqlite")


@pytest.fixture()
def ctx(tmp_path: Path, store: Store) -> Ctx:
    settings = Settings(Path.cwd())  # project config.yaml, real .env NOT required
    return Ctx(settings=settings, store=store, flags=Flags(free_only=True, verify=False))
