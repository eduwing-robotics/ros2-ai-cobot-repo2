from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.config import MaterialPrefetchMode, Settings


def test_material_prefetch_mode_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MATERIAL_PREFETCH_MODE", raising=False)
    assert Settings(_env_file=None).material_prefetch_mode is MaterialPrefetchMode.DISABLED


def test_material_prefetch_mode_accepts_one_ahead() -> None:
    assert Settings(material_prefetch_mode="one_ahead").material_prefetch_mode is MaterialPrefetchMode.ONE_AHEAD


def test_material_prefetch_mode_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        Settings(material_prefetch_mode="aggressive")
