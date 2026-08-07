import unittest
from pathlib import Path


APP_JS = (Path(__file__).parents[1] / "static" / "app.js").read_text(encoding="utf-8")
STYLE_CSS = (Path(__file__).parents[1] / "static" / "style.css").read_text(encoding="utf-8")
INDEX_HTML = (Path(__file__).parents[1] / "templates" / "index.html").read_text(encoding="utf-8")


def function_source(name, next_name):
    start = APP_JS.index(f"function {name}")
    end = APP_JS.index(f"function {next_name}", start)
    return APP_JS[start:end]


class FrontendRefreshContractTests(unittest.TestCase):
    def test_clearing_search_invalidates_inflight_suggestion_request(self):
        source = function_source("loadSymbolSuggestions", "closeSuggestionPanels")
        self.assertLess(source.index("requestId=++suggestionRequest"), source.index("if(!normalized)"))

    def test_clicking_outside_closes_market_and_watch_suggestions(self):
        source = function_source("closeSuggestionPanels", "selectSymbolSuggestion")
        self.assertIn(".search-suggest-wrap .symbol-suggestions", source)
        self.assertIn("thoughtWatchAddSuggestions", source)
        self.assertIn("document.addEventListener('click',closeSuggestionPanels)", APP_JS)

    def test_market_refresh_morphs_existing_dom_instead_of_replacing_rows(self):
        source = function_source("replaceHtmlKeepingExchangeLogos", "coinglassKlineUrl")
        self.assertIn("syncRenderedChildren", source)
        self.assertNotIn("replaceChildren", source)

    def test_market_rows_are_reused_by_stable_exchange_path_key(self):
        keyed_sync = function_source("syncKeyedRenderedChildren", "replaceHtmlKeepingExchangeLogos")
        self.assertIn("row.dataset.renderKey", keyed_sync)
        self.assertIn("currentRows.filter(row=>!row.dataset.renderKey)", keyed_sync)
        self.assertIn("currentParent.insertBefore(row,cursor)", keyed_sync)
        spot_rows = function_source("spotRows", "showMarketRefreshCountdown")
        dual_rows = function_source("dualRowsCompact", "positionFloatingPopup")
        self.assertIn('data-render-key="spot|${group.symbol}|${row.long_exchange}"', spot_rows)
        self.assertIn('data-render-key="dual|${group.symbol}|${row.long_exchange}|${row.short_exchange}"', dual_rows)

    def test_spot_sorting_uses_one_title_dropdown(self):
        picker = function_source("spotSortPickerMarkup", "configureSpotLayoutShell")
        shell = function_source("configureSpotLayoutShell", "updateSortHeaders")
        self.assertIn('id="spotSortSelect"', picker)
        self.assertIn("spotSortOptions.map", picker)
        self.assertIn(".change-sort-controls", shell)
        self.assertIn(".funding-sort-controls", shell)
        self.assertIn(".sort-state-row", shell)
        for key in (
            "open_spread", "close_spread", "basis", "funding_30d", "change_7d",
        ):
            self.assertIn(key, APP_JS)

    def test_dual_liquidity_boxes_use_bounded_flexible_columns(self):
        self.assertIn('class="dual-liquidity-row"', APP_JS)
        self.assertIn(".dual-liquidity-row>dd{min-width:0;width:100%}", STYLE_CSS)
        self.assertIn("grid-template-columns:minmax(0,1fr) auto minmax(0,1fr)", STYLE_CSS)

    def test_dual_funding_history_is_nested_below_result(self):
        source = function_source("dualRowsCompact", "positionFloatingPopup")
        self.assertIn('class="dual-result-stack"', source)
        self.assertIn("${dualResultPanel(row)}${dualFundingHistory(row)}", source)
        self.assertNotIn("dual-funding-history-cell", source)

    def test_dual_sorting_uses_one_title_dropdown(self):
        source = function_source("dualSortPickerMarkup", "configureDualLayoutShell")
        self.assertIn('id="dualSortSelect"', source)
        self.assertIn("dualSortOptions.map", source)
        for key in (
            "open_spread", "close_spread", "funding_difference_30d",
            "binance_basis", "bybit_basis", "okx_basis", "binance_change_7d",
        ):
            self.assertIn(key, APP_JS)

    def test_legacy_funding_column_injector_is_disabled(self):
        source = function_source("ensureDualFundingHistoryHeader", "dualLiquidity")
        self.assertEqual(source.strip(), "function ensureDualFundingHistoryHeader(){}")

    def test_dual_group_divider_matches_five_column_layout(self):
        source = function_source("applyGroupFrameClasses", "installGroupFrameObservers")
        self.assertIn("columns=prefix==='spot'?11:5", source)

    def test_symbol_detail_funding_query_uses_dates_and_both_exchange_sides(self):
        request_source = function_source("detailFundingRequest", "fundingEventLegs")
        load_source = function_source("loadSymbolFunding", "openSymbolDetail")
        self.assertIn("start,end", request_source)
        self.assertIn("funding_long_exchange:longExchange", request_source)
        self.assertIn("funding_short_exchange:shortExchange", request_source)
        self.assertIn("detailFundingStart", load_source)
        self.assertIn("detailFundingEnd", load_source)
        self.assertIn("detailFundingLongExchange", load_source)
        self.assertIn("detailFundingShortExchange", load_source)
        self.assertIn("renderSymbolFunding(data)", load_source)

    def test_symbol_detail_funding_refresh_updates_only_funding_panel(self):
        source = function_source("loadSymbolFunding", "openSymbolDetail")
        self.assertNotIn("modal.innerHTML", source)
        self.assertNotIn("openSymbolDetail(", source)
        self.assertIn("requestId=++symbolFundingRequest", source)
        self.assertIn("requestId===symbolFundingRequest", source)

    def test_symbol_detail_funding_defaults_and_exchange_options(self):
        options = function_source("detailFundingExchangeOptions", "detailFundingExchangeCode")
        modal = function_source("openSymbolDetail", "closeSymbolDetail")
        self.assertIn("['Bybit','BY'],['OKX','OK'],['Binance','BN']", options)
        self.assertIn("[['','无']]", options)
        self.assertIn("priorLong=''", modal)
        self.assertIn("priorShort='Binance'", modal)
        self.assertIn("openRequest=++symbolFundingRequest", modal)
        self.assertIn("openRequest!==symbolFundingRequest", modal)
        self.assertIn('id="detailFundingLongExchange" onchange="loadSymbolFunding()"', modal)
        self.assertIn('id="detailFundingShortExchange" onchange="loadSymbolFunding()"', modal)
        self.assertIn("做空交易所", modal)
        self.assertIn("grid-template-columns:90px 90px 180px 180px auto", STYLE_CSS)

    def test_symbol_detail_single_mode_only_labels_short_funding(self):
        source = function_source("renderSymbolFunding", "fetchSymbolDetail")
        self.assertIn("做空 · 单所资费", source)
        self.assertIn("仅显示做空端原始资费", source)
        self.assertIn("净值按空端资费－多端资费计算", source)

    def test_symbol_detail_contract_exchange_shows_interval_badge(self):
        source = function_source("detailExchange", "detailRows")
        self.assertIn("funding_interval_hours", source)
        self.assertIn('class="interval-badge detail-interval', source)
        self.assertIn("intervalClass(hours)", source)
        self.assertIn("待同步", source)

    def test_dashboard_renders_top_ten_momentum_scores(self):
        source = function_source("loadDashboard", "dailyTrendItem")
        self.assertIn("renderMomentumOpportunities", source)
        self.assertIn("momentum_opportunities", source)
        self.assertIn('id="momentumOpportunities"', INDEX_HTML)
        self.assertIn("涨势 / 30", INDEX_HTML)
        self.assertIn("持仓 / 25", INDEX_HTML)
        self.assertIn("人数比 / 20", INDEX_HTML)
        self.assertIn("CVD / 25", INDEX_HTML)

    def test_momentum_board_marks_ratio_rise_and_selling_cvd_red(self):
        direction = function_source("momentumDirectionClass", "momentumReasonMarkup")
        row = function_source("momentumOpportunityItem", "renderMomentumOpportunities")
        self.assertIn("type==='ratio'?value>0:value<=0", direction)
        self.assertIn("momentumDirectionClass(item,'ratio')", row)
        self.assertIn("momentumDirectionClass(item,'cvd')", row)
        self.assertIn("momentumReasonMarkup(item)", row)
        self.assertIn(".momentum-components .risk-negative b", STYLE_CSS)
        self.assertIn(".momentum-row>p .risk-negative", STYLE_CSS)

    def test_dashboard_places_oi_market_cap_ranking_beside_momentum(self):
        source = function_source("loadDashboard", "dailyTrendItem")
        self.assertIn("renderOiMarketCapOpportunities", source)
        self.assertIn("oi_market_cap_opportunities", source)
        self.assertIn('class="opportunity-signal-grid"', INDEX_HTML)
        self.assertIn('id="oiMarketCapOpportunities"', INDEX_HTML)
        self.assertIn("三所合约 OI 名义价值 / CoinGecko 流通市值", INDEX_HTML)
        self.assertIn("grid-template-columns:minmax(0,1fr) minmax(0,1fr)", STYLE_CSS)

    def test_gainers_rows_include_structure_score(self):
        shell = function_source("renderGainersShell", "gainersItem")
        row = function_source("gainersItem", "loadGainers")
        self.assertIn("结构分", shell)
        self.assertIn("item.score", row)
        self.assertIn("gainer-score", row)

    def test_binance_history_backfill_avoids_cloud_blocked_time_parameters(self):
        app_source = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
        start = app_source.index("def sync_funding_history")
        end = app_source.index("def funding_statistics", start)
        source = app_source[start:end]
        self.assertIn('{"symbol": symbol, "limit": 200}', source)
        self.assertIn('params["endTime"] = cursor_end', source)
        self.assertNotIn('"startTime": recent_start', source)


if __name__ == "__main__":
    unittest.main()
