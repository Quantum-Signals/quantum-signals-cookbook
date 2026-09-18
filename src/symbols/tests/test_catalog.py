import pytest

from qs_trader_symbols import (
    INSTRUMENTS,
    active_canonical_symbols,
    alpaca_symbol_map,
    canonical_symbols,
    canonicalize,
    is_symbol_allowed,
    parse_allowed_symbols,
    position_symbol_to_canonical,
    rithmic_root_specs,
    rithmic_symbol_map,
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


def test_rithmic_map_uses_micro_roots():
    m = rithmic_symbol_map()
    assert m["ES"]["root"] == "MES"
    assert m["YM"]["root"] == "MYM"
    assert m["CL"]["exchange"] == "NYMEX"
    assert m["Silver"]["root"] == "SI"
    assert m["Gas"]["root"] == "NG"


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
def test_every_alias_maps_to_same_rithmic_root(instrument):
    if not instrument.rithmic_root:
        pytest.skip("not on Rithmic")
    m = rithmic_symbol_map()
    for alias in instrument.aliases:
        assert m[alias]["root"] == instrument.rithmic_root
        assert m[alias]["exchange"] == instrument.rithmic_exchange


@pytest.mark.parametrize("instrument", INSTRUMENTS, ids=lambda i: i.canonical)
def test_allowed_etf_or_future_name_accepts_other_aliases(instrument):
    if not instrument.alpaca_ticker:
        pytest.skip("no ETF alias")
    allowed = parse_allowed_symbols([instrument.alpaca_ticker])
    assert allowed is not None
    for alias in instrument.aliases:
        assert is_symbol_allowed(alias, allowed)


def test_rithmic_root_specs_covers_all_futures():
    roots = {root for root, _exchange in rithmic_root_specs()}
    expected = {i.rithmic_root for i in INSTRUMENTS if i.rithmic_root}
    assert roots == expected
