"""Live Polymarket client selection (POLYMARKET_CLIENT)."""

import pytest

from execution.clients.polymarket import PolymarketExecutionClient
from execution.clients.polymarket_v2 import PolymarketExecutionClientV2
from execution.factory import _make_polymarket_client, _make_single_execution_client


def test_v2_is_the_default(monkeypatch):
    monkeypatch.delenv("POLYMARKET_CLIENT", raising=False)
    assert isinstance(_make_polymarket_client(None), PolymarketExecutionClientV2)


def test_legacy_escape_hatch(monkeypatch):
    monkeypatch.setenv("POLYMARKET_CLIENT", "legacy")
    client = _make_polymarket_client(None)
    assert isinstance(client, PolymarketExecutionClient)
    assert not isinstance(client, PolymarketExecutionClientV2)


def test_unknown_value_is_rejected(monkeypatch):
    monkeypatch.setenv("POLYMARKET_CLIENT", "v3")
    with pytest.raises(ValueError):
        _make_polymarket_client(None)


def test_single_platform_live_polymarket_uses_selector(monkeypatch):
    monkeypatch.delenv("POLYMARKET_CLIENT", raising=False)
    client = _make_single_execution_client(None, "live", "polymarket")
    assert isinstance(client, PolymarketExecutionClientV2)


def test_paper_mode_unchanged():
    from execution.clients.paper import PaperExecutionClient

    assert isinstance(
        _make_single_execution_client(None, "paper", "polymarket"),
        PaperExecutionClient,
    )
