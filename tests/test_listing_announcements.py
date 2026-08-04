import unittest
from datetime import datetime
from pathlib import Path

from app import (
    announcement_links,
    parse_announcement_effective_at,
    parse_announcement_published_at,
)


class ListingAnnouncementTests(unittest.TestCase):
    def test_numeric_utc_time_is_converted_to_utc_plus_8(self):
        page = """
        <article>Bitget will delist futures trading for IPUSDT from
        2026-06-28 07:00（UTC+0）.</article>
        """
        self.assertEqual(
            parse_announcement_effective_at(page, "下架"),
            datetime(2026, 6, 28, 15, 0),
        )

    def test_month_name_and_timezone_before_time_are_parsed(self):
        page = """
        <article>Bitget will delist SUMIELECUSDT and CRWDUSDT on
        June 26, 2026 (UTC+8), 3:00 PM.</article>
        """
        self.assertEqual(
            parse_announcement_effective_at(page, "下架"),
            datetime(2026, 6, 26, 15, 0),
        )

    def test_delisting_time_beats_the_earlier_opening_suspension(self):
        page = """
        <article>
          Bitget will suspend the opening of new SATSSTOCKUSDT positions
          from June 28, 2026, 12:00 PM (UTC+8).
          Users must close positions by June 28, 2026, 3:00 PM (UTC+8),
          when Bitget will delist futures trading.
        </article>
        """
        self.assertEqual(
            parse_announcement_effective_at(page, "下架"),
            datetime(2026, 6, 28, 15, 0),
        )

    def test_listing_time_and_published_metadata_are_separate(self):
        page = """
        <script type="application/ld+json">
          {"datePublished":"2026-07-27T08:02:03+08:00"}
        </script>
        <article>Bitget will list KUAISHOUUSDT and open trading on
        July 27, 2026, 10:00 AM (UTC+8).</article>
        """
        self.assertEqual(
            parse_announcement_published_at(page),
            datetime(2026, 7, 27, 8, 2, 3),
        )
        self.assertEqual(
            parse_announcement_effective_at(page, "上架"),
            datetime(2026, 7, 27, 10, 0),
        )

    def test_list_page_keeps_the_official_detail_url(self):
        page = """
        <a href="/support/articles/12560603887528">
          [Important] Bitget to delist IPUSDT, IPUSDC futures
        </a>
        """
        rows = announcement_links(page, "https://www.bitget.com/support/announcement-center")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "下架")
        self.assertEqual(rows[0]["url"], "https://www.bitget.com/support/articles/12560603887528")

    def test_market_pages_share_delisting_warnings_and_stable_exchange_icons(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        styles = (root / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn("/api/market-metadata/delisted-symbols", script)
        self.assertIn("replaceHtmlKeepingExchangeLogos(byId('spotFuturesRows')", script)
        self.assertIn("replaceHtmlKeepingExchangeLogos(byId('dualFuturesRows')", script)
        self.assertIn("data-market-symbol", script)
        self.assertIn(".delisting-symbol::before", styles)
        self.assertIn('content:"▲"', styles)


if __name__ == "__main__":
    unittest.main()
