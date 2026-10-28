# -*- coding: utf-8 -*-
"""Regression tests for India (NSE / BSE) suffix-only market support.

Mirrors tests/test_tw_market_support.py. Indian stocks use the Yahoo Finance
suffix forms ``SYMBOL.NS`` (NSE) and ``SYMBOL.BO`` (BSE). Unlike JP/KR/TW the
base is a ticker symbol (``RELIANCE``, ``M&M``, ``BAJAJ-AUTO``) or a numeric BSE
scrip code (``500180``), so the market rule matches a symbol pattern instead of
a digit length. Bare codes keep their existing semantics (India is strict
suffix-only).
"""

from datetime import date
from unittest.mock import patch

import pandas as pd
from data_provider.base import BaseFetcher, DataFetchError, DataFetcherManager, normalize_stock_code
from data_provider.yfinance_fetcher import YfinanceFetcher
from src.core.trading_calendar import (
    MARKET_EXCHANGE,
    MARKET_TIMEZONE,
    get_market_for_stock,
    is_market_open,
)
from src.market_context import detect_market, get_market_guidelines, get_market_role
from src.services.market_symbol_utils import get_suffix_market, is_in_suffix_symbol
from src.services.stock_code_utils import normalize_code, resolve_daily_stock_identity
from src.services.stock_list_parser import ParseStatus, parse_analysis_target


class _FakeFetcher(BaseFetcher):
    def __init__(self, name: str, should_fail: bool = False):
        self.name = name
        self.priority = 0 if name != "YfinanceFetcher" else 4
        self.calls = []
        self.should_fail = should_fail

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        raise NotImplementedError

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        raise NotImplementedError

    def get_daily_data(self, stock_code, start_date=None, end_date=None, days=30):
        self.calls.append(stock_code)
        if self.should_fail:
            raise DataFetchError(f"{self.name} should not be called for {stock_code}")
        return pd.DataFrame(
            {
                "date": [pd.Timestamp("2026-10-27")],
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "volume": [100],
                "amount": [100.0],
                "pct_chg": [0.0],
            }
        )


def test_suffix_market_accepts_nse_and_bse_symbol_shapes() -> None:
    for code in ("YESBANK.NS", "yesbank.ns", "M&M.NS", "BAJAJ-AUTO.NS", "ITC.NS", "500180.BO", "RELIANCE.BO"):
        assert get_suffix_market(code) == "in", code
        assert is_in_suffix_symbol(code), code


def test_suffix_market_rejects_bare_and_malformed_indian_codes() -> None:
    # Strict suffix-only: bare symbols / numbers keep their old semantics.
    assert get_suffix_market("RELIANCE") is None
    assert get_suffix_market("500180") is None
    # Malformed bases are rejected.
    assert get_suffix_market(".NS") is None
    assert get_suffix_market("-ABC.NS") is None
    assert get_suffix_market("AB C.NS") is None
    assert get_suffix_market("ABCDEFGHIJKLMNOPQRSTU.NS") is None  # 21 chars


def test_existing_suffix_markets_are_unchanged() -> None:
    assert get_suffix_market("7203.T") == "jp"
    assert get_suffix_market("005930.KS") == "kr"
    assert get_suffix_market("2330.TW") == "tw"
    assert get_suffix_market("TOYOTA.T") is None  # JP still requires digits


def test_normalize_and_detect_in_suffix_codes() -> None:
    assert normalize_stock_code("yesbank.ns") == "YESBANK.NS"
    assert normalize_stock_code("m&m.ns") == "M&M.NS"
    assert normalize_stock_code("500180.bo") == "500180.BO"

    # Short NSE symbols must not be mistaken for US tickers (ITC.NS looks
    # like a US ``XXX.YY`` shape) or default to A-shares (YESBANK.NS).
    assert detect_market("ITC.NS") == "in"
    assert detect_market("YESBANK.NS") == "in"
    assert detect_market("500180.BO") == "in"

    assert get_market_for_stock("ITC.NS") == "in"
    assert get_market_for_stock("500180.BO") == "in"

    assert normalize_code("yesbank.ns") == "YESBANK.NS"


def test_stock_list_parser_keeps_indian_suffix_and_exchange() -> None:
    target = parse_analysis_target("m&m.ns")
    assert target.asset_type == ParseStatus.STOCK
    assert target.canonical_id == "M&M.NS"
    assert target.exchange == "NS"

    bse = parse_analysis_target("500180.BO")
    assert bse.asset_type == ParseStatus.STOCK
    assert bse.canonical_id == "500180.BO"
    assert bse.exchange == "BO"


def test_daily_identity_resolves_to_india_market() -> None:
    identity = resolve_daily_stock_identity("YESBANK.NS")
    assert identity is not None
    assert identity.market == "in"
    assert identity.code_candidates == ("YESBANK.NS",)


def test_market_role_and_guidelines_use_indian_context() -> None:
    assert get_market_role("ITC.NS", "en") == "Indian stock"
    guidelines_en = get_market_guidelines("ITC.NS", "en")
    assert "INR" in guidelines_en
    assert "Nifty 50" in guidelines_en
    assert "A-share" in guidelines_en  # explicit "do not apply A-share concepts"
    guidelines_zh = get_market_guidelines("ITC.NS", "zh")
    assert "印度" in guidelines_zh


def test_yfinance_keeps_in_suffix_codes_and_indices() -> None:
    fetcher = YfinanceFetcher()
    assert fetcher._convert_stock_code("M&M.NS") == "M&M.NS"
    assert fetcher._convert_stock_code("yesbank.ns") == "YESBANK.NS"
    assert fetcher._convert_stock_code("500180.BO") == "500180.BO"

    fetched = []

    def _fake_fetch(yf, symbol, name, code):
        fetched.append(symbol)
        return {"code": code, "name": name, "current": 1.0}

    with patch.object(fetcher, "_fetch_yf_ticker_data", side_effect=_fake_fetch):
        indices = fetcher.get_main_indices(region="in")

    assert fetched == ["^NSEI", "^BSESN", "^NSEBANK"]
    assert [item["name"] for item in indices] == ["Nifty 50", "BSE Sensex", "Nifty Bank"]


def test_data_fetcher_manager_routes_in_daily_only_to_yfinance() -> None:
    efinance = _FakeFetcher("EfinanceFetcher", should_fail=True)
    akshare = _FakeFetcher("AkshareFetcher", should_fail=True)
    yfinance = _FakeFetcher("YfinanceFetcher")
    manager = DataFetcherManager(fetchers=[efinance, akshare, yfinance])

    with patch("data_provider.base.record_provider_run_started"), patch("data_provider.base.record_provider_run"):
        ns_df, ns_source = manager.get_daily_data("YESBANK.NS")
        bo_df, bo_source = manager.get_daily_data("500180.BO")

    assert ns_source == "YfinanceFetcher"
    assert bo_source == "YfinanceFetcher"
    assert not ns_df.empty and not bo_df.empty
    assert efinance.calls == []
    assert akshare.calls == []
    assert yfinance.calls == ["YESBANK.NS", "500180.BO"]


def test_trading_calendar_registers_in_exchange_and_timezone() -> None:
    assert MARKET_EXCHANGE["in"] == "XBOM"
    assert MARKET_TIMEZONE["in"] == "Asia/Kolkata"
    # Republic Day is an NSE/BSE holiday; the next day is a normal session.
    assert is_market_open("in", date(2026, 1, 26)) is False
    assert is_market_open("in", date(2026, 1, 27)) is True


def test_in_is_first_class_on_write_paths() -> None:
    """India must be accepted on the decision-signal / portfolio / intelligence
    write paths.

    Regression guard: the analysis pipeline auto-extracts a DecisionSignal after
    history save. If ``in`` were absent from VALID_MARKETS, _normalize_market("in")
    would raise and the signal would be silently dropped, leaving the prediction
    checker with nothing to score for Indian stocks.
    """
    from src.services.decision_signal_service import DecisionSignalService
    from src.services.intelligence_service import _ALLOWED_MARKETS
    from src.services.portfolio_service import VALID_MARKETS

    assert "in" in VALID_MARKETS
    assert "in" in _ALLOWED_MARKETS
    assert DecisionSignalService._normalize_market("in") == "in"
