"""
test_screener_engine.py - tests for the screener engine and its Python port.

The suite is deliberately OFFLINE. Every test either:

  * exercises a pure function (numbers / parser / filters), or
  * drives ``ScreenerClient`` against a monkeypatched ``_session.get`` that
    returns saved HTML, so the screener's full request -> parse -> filter path
    runs without touching screener.in.

Why no live-network test in the unit suite: a test that depends on a
third-party website is not a test, it is a coin flip. The live check lives in
``Temp/live_screener_check.py`` and is run manually as an acceptance step.

The tests that matter most are the ones asserting that a MISSING metric is not
treated as zero - see ``TestThreeValuedRules``. Those encode the safety property
described in ``screener/filters.py``.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Every test module in this suite puts the Blue_engine root on sys.path
# explicitly (see test_recovery.py). Without it, `blue_engine.*` only resolves
# when pytest happens to add the root, which is not guaranteed across runners.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from blue_engine.screener.client import parser as P
from blue_engine.screener.client.numbers import (
    CRORE,
    latest_period_value,
    ratio_lookup,
    table_row_lookup,
    to_number,
)
from blue_engine.screener.client.screener_client import ScreenerClient
from blue_engine.screener.filters import (
    FAIL,
    PASS,
    UNKNOWN,
    Rule,
    Screener,
    default_rules,
)
from blue_engine.screener.screener_engine import ScreenerEngine

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "screener"


# --------------------------------------------------------------------------- #
# numbers.to_number
# --------------------------------------------------------------------------- #
class TestToNumber:
    @pytest.mark.parametrize("raw,expected", [
        ("500 Cr.", 500.0),
        ("5,00,000 Cr.", 500000.0),      # Indian lakh/crore grouping
        ("\u20b91,23,456 Cr.", 123456.0),  # rupee sign
        ("18.5", 18.5),
        ("1,234.56", 1234.56),
        ("12.3%", 12.3),
        ("-35.09", -35.09),              # negative growth is preserved
        ("+7.5", 7.5),
    ])
    def test_parses_numbers(self, raw, expected):
        assert to_number(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", ["", "-", "--", "N/A", "n/a", "None",
                                     "\u2014", "not available"])
    def test_null_tokens_are_none(self, raw):
        """The whole safety story: a missing cell is None, never 0.0."""
        assert to_number(raw) is None

    def test_none_and_bool_inputs(self):
        assert to_number(None) is None
        # bool is an int subclass; True in a numeric field is a bug, not a 1.
        assert to_number(True) is None
        assert to_number(False) is None

    def test_numeric_passthrough(self):
        assert to_number(42) == 42.0
        assert to_number(3.5) == 3.5

    def test_suffix_units(self):
        assert to_number("50 Lakh", unit_scale="cr") == pytest.approx(0.5)
        assert to_number("1 Mn", unit_scale="cr") == pytest.approx(0.1)
        assert to_number("2 Cr.", unit_scale="cr") == pytest.approx(2.0)

    def test_absolute_unit(self):
        """'1 Cr.' must become 10,000,000 rupees - the crore multiplier is 1.0
        so the suffix DETECTION must not be inferred from the multiplier."""
        assert to_number("1 Cr.", unit_scale="absolute") == pytest.approx(CRORE)
        assert to_number("50 Lakh", unit_scale="absolute") == pytest.approx(CRORE * 0.5)

    def test_absolute_refuses_bare_number(self):
        """A bare '1234' has no magnitude. Guessing crores would corrupt a
        market-cap filter, so it must refuse rather than invent."""
        assert to_number("1234", unit_scale="absolute") is None

    def test_garbage_is_none(self):
        assert to_number("Debt to equity") is None
        assert to_number("abc") is None


# --------------------------------------------------------------------------- #
# numbers.ratio_lookup
# --------------------------------------------------------------------------- #
class TestRatioLookup:
    RATIOS = [
        {"id": "market-cap", "name": "Market Cap", "value": "\u20b95,00,000 Cr."},
        {"id": "current-price", "name": "Current Price", "value": "\u20b91,750"},
        {"id": "stock-pe", "name": "Stock P/E", "value": "18.5"},
        {"id": "roe", "name": "ROE", "value": "16.5%"},
    ]

    def test_lookup_by_id(self):
        assert ratio_lookup(self.RATIOS, "market_cap") == "\u20b95,00,000 Cr."

    def test_lookup_by_name_when_id_absent(self):
        rows = [{"name": "Stock P/E", "value": "18.5"}]
        assert ratio_lookup(rows, "pe") == "18.5"

    def test_name_alias_variants(self):
        assert ratio_lookup([{"name": "P/E", "value": "9"}], "pe") == "9"

    def test_missing_metric_is_none(self):
        assert ratio_lookup(self.RATIOS, "debt_equity") is None

    def test_null_value_is_skipped(self):
        rows = [{"id": "roe", "name": "ROE", "value": "-"}]
        assert ratio_lookup(rows, "roe") is None

    def test_unknown_metric_is_none(self):
        assert ratio_lookup(self.RATIOS, "not_a_metric") is None


# --------------------------------------------------------------------------- #
# parser (HTML -> Python)
# --------------------------------------------------------------------------- #
class TestParser:
    HTML = """
    <html><head><title>Reliance Industries - Balance Sheet, Annual Report</title></head>
    <body>
      <ul id="top-ratios">
        <li id="market-cap"><span class="name">Market Cap</span>
            <span class="value">\u20b917,00,000 Cr.</span></li>
        <li id="stock-pe"><span class="name">Stock P/E</span>
            <span class="value">24.8</span></li>
      </ul>
      <section id="analysis">
        <div class="pros"><ul><li>Good ROE track record</li></ul></div>
        <div class="cons"><ul><li>Trading at high book value</li></ul></div>
        <p>A large Indian conglomerate.</p>
      </section>
      <section id="ratios">
        <table>
          <thead><tr><th></th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
          <tbody>
            <tr><td>Debt to equity</td><td>0.45</td><td>0.38</td></tr>
            <tr><td>ROE %</td><td>9.1</td><td>10.4</td></tr>
          </tbody>
        </table>
      </section>
      <section id="documents">
        <a href="/ar/2025.pdf">Annual Report 2025</a>
      </section>
    </body></html>
    """

    def setup_method(self):
        self.root = P.parse_html(self.HTML)

    def test_title(self):
        assert P.parse_title(self.root) == "Reliance Industries"

    def test_top_ratios(self):
        ratios = P.parse_top_ratios(self.root)
        assert len(ratios) == 2
        assert ratios[0]["id"] == "market-cap"
        assert ratios[0]["name"] == "Market Cap"
        assert "17,00,000" in ratios[0]["value"]

    def test_table(self):
        table = P.parse_table(self.root.select_one("section#ratios"))
        assert table is not None
        assert table["headers"] == ["", "Mar 2024", "Mar 2025"]
        assert table["rows"][0][""] == "Debt to equity"
        assert table["rows"][0]["Mar 2025"] == "0.38"

    def test_missing_section_is_none(self):
        assert P.parse_table(self.root.select_one("section#does-not-exist")) is None

    def test_analysis(self):
        a = P.parse_analysis(self.root)
        assert a["pros"] == ["Good ROE track record"]
        assert a["cons"] == ["Trading at high book value"]
        assert a["description"] == "A large Indian conglomerate."

    def test_documents(self):
        docs = P.parse_documents(self.root)
        assert docs == [{"title": "Annual Report 2025", "url": "/ar/2025.pdf"}]

    def test_section_ids(self):
        ids = P.parse_section_ids(self.root)
        assert "ratios" in ids and "analysis" in ids

    def test_data_table_handles_tbody_header(self):
        """screener.in sometimes puts the header row inside tbody with <th>."""
        html = """
        <table><tbody>
          <tr><th>Name</th><th>Mar Cap</th><th>P/E</th></tr>
          <tr><td>TCS</td><td>14,00,000</td><td>31.2</td></tr>
        </tbody></table>
        """
        tbl = P.parse_data_table(P.parse_html(html))
        assert tbl["columns"] == ["Name", "Mar Cap", "P/E"]
        assert len(tbl["rows"]) == 1
        assert tbl["rows"][0]["Name"] == "TCS"

    def test_screen_items(self):
        html = """
        <a href="/screens/23/undervalued">Undervalued
           Stocks trading below intrinsic value</a>
        """
        items = P.parse_screen_items(P.parse_html(html))
        assert items[0]["id"] == 23
        assert items[0]["slug"] == "undervalued"
        assert items[0]["title"] == "Undervalued"

    def test_pagination(self):
        html = '<div class="pagination"><a href="?page=3">3</a></div>'
        assert P.parse_pagination(P.parse_html(html))["total_pages"] == 3


# --------------------------------------------------------------------------- #
# filters: Rule and Screener
# --------------------------------------------------------------------------- #
class TestRule:
    def test_shorthand_min(self):
        r = Rule.from_dict({"metric": "market_cap", "min": 200})
        assert (r.metric, r.op, r.threshold) == ("market_cap", ">", 200.0)

    def test_shorthand_max_inclusive(self):
        r = Rule.from_dict({"metric": "pe", "max_inclusive": 30})
        assert (r.op, r.threshold) == ("<=", 30.0)

    def test_explicit_form(self):
        r = Rule.from_dict({"metric": "debt_equity", "op": "<", "threshold": 1})
        assert (r.op, r.threshold) == ("<", 1.0)

    def test_bad_op_raises_at_construction(self):
        """A typo in a rule must fail loudly, not silently skip the rule."""
        with pytest.raises(ValueError):
            Rule.from_dict({"metric": "pe", "op": "=<", "threshold": 10})

    def test_no_threshold_raises(self):
        with pytest.raises(ValueError):
            Rule.from_dict({"metric": "pe"})


class TestThreeValuedRules:
    """A rule returns PASS / FAIL / UNKNOWN. UNKNOWN must never be a pass."""

    def test_pass(self):
        r = Rule("market_cap", ">", 200)
        assert r.evaluate({"market_cap": "500 Cr."}).outcome == PASS

    def test_fail(self):
        r = Rule("market_cap", ">", 200)
        assert r.evaluate({"market_cap": "150 Cr."}).outcome == FAIL

    def test_unknown_on_dash(self):
        r = Rule("debt_equity", "<", 1)
        assert r.evaluate({"debt_equity": "-"}).outcome == FAIL

    def test_unknown_on_absent_key(self):
        r = Rule("debt_equity", "<", 1)
        assert r.evaluate({}).outcome == FAIL

    def test_on_missing_unknown_policy(self):
        r = Rule("debt_equity", "<", 1, on_missing="unknown")
        assert r.evaluate({}).outcome == UNKNOWN

    def test_on_missing_pass_policy(self):
        r = Rule("debt_equity", "<", 1, on_missing="pass")
        assert r.evaluate({}).outcome == PASS

    def test_boundary_is_strict(self):
        """Exactly 200 Cr and exactly D/E 1.0 must FAIL (strict inequalities)."""
        assert Rule("market_cap", ">", 200).evaluate(
            {"market_cap": "200 Cr."}).outcome == FAIL
        assert Rule("debt_equity", "<", 1).evaluate(
            {"debt_equity": "1.0"}).outcome == FAIL


class TestScreener:
    def _screen(self, **kw):
        return Screener(default_rules(), name="t", **kw)

    def test_the_requested_screen(self):
        screen = self._screen()
        out = screen.evaluate([
            {"symbol": "GOOD", "metrics": {"market_cap": "500 Cr.",
                                           "debt_equity": "0.4"}},
            {"symbol": "SMALLCAP", "metrics": {"market_cap": "150 Cr.",
                                               "debt_equity": "0.4"}},
            {"symbol": "LEVERED", "metrics": {"market_cap": "500 Cr.",
                                              "debt_equity": "1.5"}},
            {"symbol": "NODATA", "metrics": {"market_cap": "500 Cr.",
                                             "debt_equity": "-"}},
        ])
        by = {r.symbol: r for r in out}
        assert by["GOOD"].selected is True
        assert by["SMALLCAP"].selected is False
        assert by["LEVERED"].selected is False
        assert by["NODATA"].selected is False

        # The reason strings are what the operator sees; they must be specific.
        assert "150" in by["SMALLCAP"].reason()
        assert "1.5" in by["LEVERED"].reason()
        assert "missing" in by["NODATA"].reason().lower()

    def test_sector_exclusion(self):
        screen = self._screen(exclude_sectors=["bank", "finance"])
        out = screen.evaluate([
            {"symbol": "BANKX", "sector": "Banks",
             "metrics": {"market_cap": "9,00,000 Cr.", "debt_equity": "0.5"}},
        ])
        assert out[0].selected is False
        assert "sector" in out[0].reason().lower()

    def test_explicit_symbol_exclusion(self):
        screen = self._screen(exclude_symbols=["TATAMOTORS"])
        out = screen.evaluate([
            {"symbol": "TATAMOTORS",
             "metrics": {"market_cap": "3,00,000 Cr.", "debt_equity": "0.5"}},
        ])
        assert out[0].selected is False
        assert "exclude" in out[0].reason().lower()

    def test_min_rules_passed_ors(self):
        screen = Screener(default_rules(), name="t", min_rules_passed=1)
        out = screen.evaluate([
            {"symbol": "ONLYCAP", "metrics": {"market_cap": "500 Cr.",
                                              "debt_equity": "9"}},
        ])
        assert out[0].selected is True

    def test_no_rules_raises(self):
        with pytest.raises(ValueError):
            Screener([], name="empty")

    def test_from_config_uses_rules_block(self):
        screen = Screener.from_config({
            "name": "custom",
            "rules": [{"metric": "pe", "max": 20}],
        })
        assert screen.name == "custom"
        assert len(screen.rules) == 1
        assert screen.rules[0].metric == "pe"

    def test_from_config_falls_back_to_defaults(self):
        screen = Screener.from_config({"name": "defaults"})
        assert [(r.metric, r.op) for r in screen.rules] == [
            ("market_cap", ">"), ("debt_equity", "<")]

    def test_financial_sectors_excluded_by_default(self):
        """Code default MUST match the shipped config: financials are excluded.

        A lender's debt/equity is not a solvency measure, so applying "< 1" to
        it selects the opposite of the screen's intent. Omitting the key must
        therefore mean "use the deny list", not "apply the rule to banks".
        """
        from blue_engine.screener.filters import DEFAULT_EXCLUDE_SECTORS
        screen = Screener.from_config({"name": "defaults"})
        assert screen.exclude_sectors == list(DEFAULT_EXCLUDE_SECTORS)
        out = screen.evaluate([
            {"symbol": "BANKX", "sector": "Banks",
             "metrics": {"market_cap": "9,00,000 Cr.", "debt_equity": "0.5"}},
        ])
        assert out[0].selected is False

    def test_empty_exclude_list_is_honoured(self):
        """An EXPLICIT empty list is a deliberate choice and must be respected."""
        screen = Screener.from_config({"name": "all", "exclude_sectors": []})
        assert screen.exclude_sectors == []


# --------------------------------------------------------------------------- #
# Client against fixture HTML (no network)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def fixture_html():
    """Load the saved screener.in company page used by the client tests."""
    path = FIXTURES / "company_reliance.html"
    if not path.is_file():
        pytest.skip(f"fixture missing: {path}")
    return path.read_text(encoding="utf-8")


def _client_with(monkeypatch, html_by_url):
    """Build a ScreenerClient whose HTTP layer is a dict lookup."""
    client = ScreenerClient(cache_dir=None, min_interval_ms=0)

    class _Resp:
        def __init__(self, text, url, status=200):
            self.text = text
            self.url = url
            self.status_code = status

    def fake_get(url, **kwargs):
        for key, html in html_by_url.items():
            if key in url:
                return _Resp(html, url)
        return _Resp("", url, status=404)

    monkeypatch.setattr(client._session, "get", fake_get)
    return client


class TestClientFixtures:
    def test_get_company(self, monkeypatch, fixture_html):
        client = _client_with(monkeypatch, {"/company/RELIANCE": fixture_html})
        res = client.get_company("RELIANCE")
        assert res.ok
        assert res.data["symbol"] == "RELIANCE"
        assert res.data["top_ratios"], "top ratios should parse"
        assert res.meta.source_url.endswith("/company/RELIANCE/consolidated/")

    def test_get_company_tab(self, monkeypatch, fixture_html):
        client = _client_with(monkeypatch, {"/company/RELIANCE": fixture_html})
        res = client.get_company_tab("RELIANCE", "ratios")
        assert res.ok
        assert "ratios" in res.data

    def test_get_company_tab_rejects_unknown(self):
        client = ScreenerClient(cache_dir=None, min_interval_ms=0)
        res = client.get_company_tab("X", "not-a-tab")
        assert res.ok is False
        assert "unknown tab" in res.error

    def test_404_is_not_ok(self, monkeypatch):
        client = _client_with(monkeypatch, {})
        res = client.get_company("NOPE")
        assert res.ok is False
        assert "404" in (res.error or "")

    def test_login_wall_is_detected_not_retried(self, monkeypatch):
        """A redirect to /login must surface LOGIN_REQUIRED, never be retried."""
        client = ScreenerClient(cache_dir=None, min_interval_ms=0, max_retries=3)

        class _Resp:
            def __init__(self):
                self.text = ""
                self.url = "https://www.screener.in/login/?next=/screens/1/x/"
                self.status_code = 200

        calls = {"n": 0}

        def fake_get(url, **kwargs):
            calls["n"] += 1
            return _Resp()

        monkeypatch.setattr(client._session, "get", fake_get)
        res = client.get_screen_details(1, "x")
        assert res.ok is False
        assert "LOGIN_REQUIRED" in (res.error or "")
        assert calls["n"] == 1, "a login wall must not be retried"

    def test_cache_avoids_second_request(self, monkeypatch):
        html = "<html><head><title>TCS Ltd</title></head><body></body></html>"
        client = ScreenerClient(cache_dir=None, min_interval_ms=0)
        calls = {"n": 0}

        class _Resp:
            def __init__(self):
                self.text = html
                self.url = "https://www.screener.in/company/TCS/consolidated/"
                self.status_code = 200

        def fake_get(url, **kwargs):
            calls["n"] += 1
            return _Resp()

        monkeypatch.setattr(client._session, "get", fake_get)
        client.get_company("TCS")
        client.get_company("TCS")
        assert calls["n"] == 1, "second call should hit the cache"
        assert client.stats["cache_hits"] == 1


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class TestScreenerEngine:
    def _engine(self, **cfg):
        base = {"enabled": True, "source": "symbols", "symbols": ["RELIANCE", "TCS"]}
        base.update(cfg)
        return ScreenerEngine(config=base)

    def test_offline_provider_path(self):
        """The full engine path with zero network: candidates supplied inline."""
        engine = self._engine()
        ctx = {"screener_inputs": {"candidates": [
            {"symbol": "INFY", "name": "Infosys", "sector": "IT Software",
             "metrics": {"market_cap": "6,20,000 Cr.", "debt_equity": "0.09",
                         "pe": "24.8", "roe": "32.6%"}},
            {"symbol": "TINY", "name": "Tiny Co", "sector": "IT Software",
             "metrics": {"market_cap": "120 Cr.", "debt_equity": "0.2"}},
            {"symbol": "BANKX", "name": "A Bank", "sector": "Banks",
             "metrics": {"market_cap": "9,00,000 Cr.", "debt_equity": "0.5"}},
        ]}}
        result = engine.run(ctx)
        assert result.ok
        data = result.data
        assert data["selected"] == ["INFY"]
        assert data["counts"]["candidates"] == 3
        assert data["counts"]["selected"] == 1

        # The payload must not be mistakable for live prices.
        assert data["data_kind"] == "fundamental_snapshot"
        assert "NOT live" in data["data_date_note"] or \
               "not live" in data["data_date_note"].lower()

    def test_alpha_fundamental_mapping(self):
        """roe is a PERCENT on screener.in but a FRACTION in Alpha's rules."""
        engine = self._engine()
        ctx = {"screener_inputs": {"candidates": [
            {"symbol": "INFY", "sector": "IT Software",
             "metrics": {"market_cap": "6,20,000 Cr.", "debt_equity": "0.09",
                         "pe": "24.8", "roe": "32.6%",
                         "dividend_yield": "2.1%"}},
        ]}}
        data = engine.run(ctx).data
        f = data["fundamentals"]["INFY"]
        assert f["pe"] == pytest.approx(24.8)
        assert f["de"] == pytest.approx(0.09)        # Alpha's key is `de`
        assert f["roe"] == pytest.approx(0.326)      # 32.6% -> 0.326
        assert f["dividend_yield"] == pytest.approx(0.021)
        assert f["market_cap"] == pytest.approx(620000.0)

    def test_disabled_engine_is_a_noop(self):
        engine = self._engine(enabled=False)
        result = engine.run({})
        assert result.ok
        assert result.data["counts"]["selected"] == 0
        assert "disabled" in " ".join(result.warnings).lower()

    def test_empty_inputs_degrade_cleanly(self):
        engine = ScreenerEngine(config={"enabled": True, "source": "symbols",
                                        "symbols": []})
        result = engine.run({"screener_inputs": {"candidates": []}})
        assert result.ok
        assert result.data["counts"]["candidates"] == 0

    def test_max_symbols_cap(self):
        engine = self._engine(max_symbols=2)
        candidates = [
            {"symbol": f"S{i}", "sector": "IT Software",
             "metrics": {"market_cap": "500 Cr.", "debt_equity": "0.1"}}
            for i in range(5)
        ]
        result = engine.run({"screener_inputs": {"candidates": candidates}})
        assert result.data["counts"]["candidates"] == 2
        assert any("truncated" in w for w in result.warnings)

    def test_polite_limits_are_reported(self):
        limits = self._engine().polite_limits()
        assert limits["max_symbols"] > 0
        assert limits["min_interval_ms"] >= 200

    def test_screen_now_returns_plain_dict(self):
        engine = self._engine()
        payload = engine.screen_now(symbols=[])  # no symbols -> empty, no network
        assert isinstance(payload, dict)
        assert payload["ok"] is True

    def test_engine_never_raises_on_bad_provider(self):
        """A provider that throws must degrade, not propagate."""
        def boom():
            raise RuntimeError("provider exploded")

        engine = ScreenerEngine(config={"enabled": True}, provider=boom)
        result = engine.run({})
        assert result.ok is True          # degrades to a no-op, not a failure

    def test_stage_is_before_alpha(self):
        from blue_engine.core.base import EngineStage
        assert ScreenerEngine.stage.value < EngineStage.ALPHA.value


class TestDebtEquityDerivation:
    """screener.in publishes no debt/equity cell, so it is derived from the
    balance sheet. These tests pin the derivation and its failure modes.

    The row labels here are copied from REAL screener.in pages: ITC uses
    "Borrowings +" while HDFCBANK uses "Borrowing" (singular, no '+'). Matching
    only "borrowings" silently missed every bank.
    """

    def _engine(self):
        return ScreenerEngine(config={"enabled": True})

    @staticmethod
    def _table(rows):
        return {"headers": ["", "Mar 2024", "Mar 2025"], "rows": rows}

    def test_derives_from_borrowings_plus(self):
        """ITC-style label."""
        table = self._table([
            {"": "Equity Capital", "Mar 2024": "1,253", "Mar 2025": "1,253"},
            {"": "Reserves", "Mar 2024": "60,000", "Mar 2025": "65,000"},
            {"": "Borrowings +", "Mar 2024": "2,000", "Mar 2025": "2,200"},
        ])
        de = self._engine()._debt_equity_from_balance_sheet(table)
        assert de == pytest.approx(2200 / 66253)
        assert de < 1

    def test_derives_from_singular_borrowing(self):
        """HDFCBANK-style label: 'Borrowing', not 'Borrowings'.

        This is the regression test for the bug found on the first live run:
        the label matcher missed banks entirely, so their debt/equity came back
        unmeasurable - and on a different bank that could have produced a false
        PASS rather than a false 'missing'.
        """
        table = self._table([
            {"": "Equity Capital", "Mar 2024": "1,539", "Mar 2025": "1,539"},
            {"": "Reserves", "Mar 2024": "584,520", "Mar 2025": "584,520"},
            {"": "Borrowing", "Mar 2024": "588,485", "Mar 2025": "588,485"},
        ])
        de = self._engine()._debt_equity_from_balance_sheet(table)
        assert de == pytest.approx(588485 / 586059)
        assert de > 1, "HDFCBANK is genuinely levered above 1"

    def test_missing_borrowings_is_none_not_zero(self):
        table = self._table([
            {"": "Equity Capital", "Mar 2025": "1,539"},
            {"": "Reserves", "Mar 2025": "584,520"},
        ])
        assert self._engine()._debt_equity_from_balance_sheet(table) is None

    def test_blank_borrowings_cell_is_none(self):
        table = self._table([
            {"": "Equity Capital", "Mar 2025": "1,539"},
            {"": "Reserves", "Mar 2025": "584,520"},
            {"": "Borrowings +", "Mar 2025": "-"},
        ])
        assert self._engine()._debt_equity_from_balance_sheet(table) is None

    def test_negative_equity_is_none(self):
        """A negative-equity company has no meaningful ratio, and a small
        positive number would wrongly satisfy '< 1'."""
        table = self._table([
            {"": "Equity Capital", "Mar 2025": "100"},
            {"": "Reserves", "Mar 2025": "-500"},
            {"": "Borrowings +", "Mar 2025": "1,000"},
        ])
        assert self._engine()._debt_equity_from_balance_sheet(table) is None

    def test_empty_table_is_none(self):
        engine = self._engine()
        assert engine._debt_equity_from_balance_sheet(None) is None
        assert engine._debt_equity_from_balance_sheet({}) is None

    def test_zero_borrowings_is_a_real_zero(self):
        """Zero debt IS measurable and must pass - distinct from 'missing'."""
        table = self._table([
            {"": "Equity Capital", "Mar 2025": "100"},
            {"": "Reserves", "Mar 2025": "900"},
            {"": "Borrowings +", "Mar 2025": "0"},
        ])
        de = self._engine()._debt_equity_from_balance_sheet(table)
        assert de == 0.0
        assert de is not None

    def test_engine_records_debt_equity_source(self):
        engine = self._engine()
        metrics = engine._metrics_from_company({
            "top_ratios": [{"name": "Market Cap", "value": "500 Cr."}],
            "balance_sheet": self._table([
                {"": "Equity Capital", "Mar 2025": "100"},
                {"": "Reserves", "Mar 2025": "900"},
                {"": "Borrowings +", "Mar 2025": "250"},
            ]),
        })
        assert metrics["debt_equity"] == pytest.approx(0.25)
        assert metrics["debt_equity_source"] == "reported"

    def test_metrics_derived_documents_the_method(self):
        derived = self._engine()._metrics_derived()
        assert "debt_equity" in derived
        assert "Borrowings" in derived["debt_equity"]


# --------------------------------------------------------------------------- #
# Config file
# --------------------------------------------------------------------------- #
class TestShippedConfig:
    def test_config_is_valid_json_and_matches_code_defaults(self):
        cfg_path = (Path(__file__).resolve().parents[1]
                    / "input" / "screener_engine.json")
        assert cfg_path.is_file(), "screener_engine.json must ship with the engine"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        rules = cfg["screener"]["screen"]["rules"]
        assert [r["metric"] for r in rules] == ["market_cap", "debt_equity"]
        assert rules[0]["op"] == ">" and rules[0]["threshold"] == 200
        assert rules[1]["op"] == "<" and rules[1]["threshold"] == 1

        # The shipped config must build a screen identical to the code default.
        built = Screener.from_config(cfg["screener"]["screen"])
        expected = default_rules()
        assert [(r.metric, r.op, r.threshold) for r in built.rules] == \
               [(r.metric, r.op, r.threshold) for r in expected]

    def test_shipped_config_builds_an_engine(self):
        cfg_path = (Path(__file__).resolve().parents[1]
                    / "input" / "screener_engine.json")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        engine = ScreenerEngine(config=cfg["screener"])
        assert engine.screen.name == "mktcap200_de_lt1"
