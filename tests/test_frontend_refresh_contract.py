import unittest
from pathlib import Path


APP_JS = (Path(__file__).parents[1] / "static" / "app.js").read_text(encoding="utf-8")


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


if __name__ == "__main__":
    unittest.main()
