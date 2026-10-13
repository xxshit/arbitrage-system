import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


class HedgePositionFrontendContractTests(unittest.TestCase):
    def test_every_spot_route_has_a_hedge_builder_action(self):
        start = APP_JS.index("function spotRows")
        end = APP_JS.index("function showMarketRefreshCountdown", start)
        source = APP_JS[start:end]
        self.assertIn("建立对冲仓位", source)
        self.assertIn("openHedgePositionBuilder", source)
        self.assertIn("row.long_exchange", source)

    def test_positions_page_keeps_current_and_history_under_one_nav_item(self):
        self.assertEqual(INDEX_HTML.count('data-view="my-positions"'), 1)
        self.assertIn('id="my-positions"', INDEX_HTML)
        self.assertIn('data-position-tab="current"', INDEX_HTML)
        self.assertIn('data-position-tab="history"', INDEX_HTML)
        self.assertIn('id="hedgePositionHistory"', INDEX_HTML)

    def test_preview_cannot_submit_a_live_order(self):
        self.assertIn('id="hedgeSubmitButton"', INDEX_HTML)
        self.assertIn('disabled>等待币安私有 API', INDEX_HTML)
        self.assertNotIn("fetch('/api/hedge-positions',{method:'POST'", APP_JS)
        self.assertIn("preview_only", (ROOT / "app.py").read_text(encoding="utf-8"))

    def test_position_layout_has_mobile_rules(self):
        self.assertIn(".position-summary", STYLE_CSS)
        self.assertIn(".hedge-position-legs", STYLE_CSS)
        self.assertIn(".hedge-preview-dialog", STYLE_CSS)
        self.assertIn("@media(max-width:760px)", STYLE_CSS)


if __name__ == "__main__":
    unittest.main()
