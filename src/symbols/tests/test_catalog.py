import pytest

from qs_trader_symbols import (
    INSTRUMENTS,
    active_canonical_symbols,
    alpaca_symbol_map,
    canonical_symbols,
    canonicalize,
    ironbeam_root_exchanges,
    ironbeam_symbol_map,
    is_symbol_allowed,
    parse_allowed_symbols,
    position_symbol_to_canonical,
)


def test_all_qs_aliases_canonicalize():
    for instrument in INSTRUMENTS:
        for alias in instrument.aliases:
            assert canonicalize(alias) == instrument.canonical


def test_position_symbols_map_back():
    assert position_symbol_to_canonical("SPY") == "ES"
    assert position_symbol_to_canonical("MES") == "ES"
    assert position_symbol_to_canonical("QQQ") == "NQ"
    assert position_symbol_to_canonical("MNQ") == "NQ"


def test_active_canonical_symbols_deduplicates_aliases():
    assert active_canonical_symbols(["ES", "S&P500", "SPY"]) == ["ES"]
    assert active_canonical_symbols(["ES", "NQ"]) == ["ES", "NQ"]


def test_parse_allowed_symbols_accepts_etf_or_future():
    allowed = parse_allowed_symbols(["SPY", "QQQ"])
    assert allowed is not None
    assert allowed.canonical == frozenset({"ES", "NQ"})


def test_is_symbol_allowed_matches_any_alias():
    allowed = parse_allowed_symbols(["SPY"])
    assert is_symbol_allowed("ES", allowed)
    assert is_symbol_allowed("S&P500", allowed)
    assert is_symbol_allowed("SPY", allowed)
    assert not is_symbol_allowed("NQ", allowed)


def test_alpaca_map_routes_futures_and_etfs_to_same_ticker():
    m = alpaca_symbol_map()
    assert m["ES"] == m["SPY"] == m["S&P500"] == m["MES"] == "SPY"
    assert m["NQ"] == m["QQQ"] == m["Nasdaq100"] == "QQQ"


def test_alpaca_map_covers_index_aliases():
    m = alpaca_symbol_map()
    assert m["ES"] == "SPY"
    assert m["S&P500"] == "SPY"
    assert m["Nasdaq100"] == "QQQ"
    assert m["Russell2000"] == "IWM"
    assert m["AAPL"] == "AAPL"


def test_silver_and_gas_aliases():
    assert canonicalize("Silver") == "SI"
    assert canonicalize("SLV") == "SI"
    assert canonicalize("Natural Gas") == "NG"
    assert canonicalize("Gas") == "NG"
    assert position_symbol_to_canonical("UNG") == "NG"


def test_canonical_symbols_lists_all_products():
    assert "ES" in canonical_symbols()
    assert "AAPL" in canonical_symbols()
    assert len(canonical_symbols()) == len(INSTRUMENTS)


@pytest.mark.parametrize("instrument", INSTRUMENTS, ids=lambda i: i.canonical)
def test_every_alias_maps_to_same_alpaca_ticker(instrument):
    if not instrument.alpaca_ticker:
        pytest.skip("equities-only or futures-only instrument")
    m = alpaca_symbol_map()
    for alias in instrument.aliases:
        assert m[alias] == instrument.alpaca_ticker


@pytest.mark.parametrize("instrument", INSTRUMENTS, ids=lambda i: i.canonical)
def test_allowed_etf_or_future_name_accepts_other_aliases(instrument):
    if not instrument.alpaca_ticker:
        pytest.skip("no ETF alias")
    allowed = parse_allowed_symbols([instrument.alpaca_ticker])
    assert allowed is not None
    for alias in instrument.aliases:
        assert is_symbol_allowed(alias, allowed)


def test_ironbeam_micro_map():
    m = ironbeam_symbol_map(use_micros=True)
    assert m["ES"] == m["SPY"] == m["S&P500"] == "MES"
    assert m["NQ"] == "MNQ"
    assert m["YM"] == "MYM"
    assert m["RTY"] == "M2K"
    assert m["GC"] == "MGC"
    assert m["SI"] == "SIL"
    assert m["CL"] == "MCL"
    assert m["NG"] == "NG"  # no micro: falls back to full size
    assert "AAPL" not in m


def test_ironbeam_map_full_size():
    m = ironbeam_symbol_map(use_micros=False)
    assert m["ES"] == "ES" and m["Nasdaq100"] == "NQ" and m["Gold"] == "GC"


def test_ironbeam_roots_map_back_to_canonical():
    for root, canon in {"MES": "ES", "MCL": "CL", "MGC": "GC", "SIL": "SI", "NG": "NG"}.items():
        assert position_symbol_to_canonical(root) == canon
    ex = ironbeam_root_exchanges()
    assert ex["MYM"] == ex["YM"] == "XCBT"
    assert ex["MCL"] == "XNYM" and ex["SIL"] == "XCEC" and ex["MES"] == "XCME"


@pytest.mark.parametrize("micro, canonical", [("MCL", "CL"), ("MGC", "GC"), ("SIL", "SI")])
def test_energy_and_metal_micros_are_recognised(micro, canonical):
    assert canonicalize(micro) == canonical
    assert position_symbol_to_canonical(micro) == canonical
    assert alpaca_symbol_map()[micro] == alpaca_symbol_map()[canonical]
