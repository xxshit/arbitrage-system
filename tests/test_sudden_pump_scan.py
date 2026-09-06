import unittest
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

from app import (
    SUDDEN_PUMP_CONFIRMATIONS,
    classify_sudden_pump_stage,
    evaluate_push_signal_validation,
    fetch_sudden_pump_context,
    push_signal_validation_bounds,
    push_signal_validation_summary,
    send_sudden_pump_push,
    sudden_pump_bullish_confirmation,
    sudden_pump_confirmation_ready,
    sudden_pump_push_section,
)


def kline(open_time, open_price, close_price, quote_volume=1000, taker_buy_quote=550):
    high = max(open_price, close_price)
    low = min(open_price, close_price)
    return [
        open_time, open_price, high, low, close_price, 0, open_time + 299_999,
        quote_volume, 0, 0, taker_buy_quote,
    ]


def path_kline(open_epoch, high, low, close=100):
    return [
        open_epoch * 1000, 100, high, low, close, 0, open_epoch * 1000 + 299_999,
        1000, 0, 0, 550,
    ]


def validation_row(**overrides):
    values = {
        "trigger_epoch": 1001,
        "validation_minutes": 10,
        "trigger_price": 100.0,
        "direction": "up",
        "target_move_pct": 5.0,
        "failure_move_pct": 5.0,
        "status": "pending",
        "first_hit": None,
        "first_hit_at": None,
        "max_favorable_pct": 0.0,
        "max_adverse_pct": 0.0,
        "peak_price": None,
        "trough_price": None,
        "observed_candle_count": 0,
        "resolved_at": None,
        "result_note": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def bullish_pump_item(**overrides):
    values = {
        "symbol": "FAST/USDT",
        "last_price": 1.25,
        "change_5m": 18.0,
        "change_15m": 35.0,
        "hot_bar_count": 2,
        "cvd": 250_000,
        "oi_change": 1.5,
        "ratio_change": -0.8,
        "volume_ratio": 2.5,
        "closed_trigger": True,
        "trigger_bucket": 1_700_000_000,
        "stage_key": "acceleration",
        "stage_label": "短线连续加速",
        "stage_rank": 2,
        "stage_reason": "短线连续扩张。",
    }
    values.update(overrides)
    return values


class SuddenPumpScanTests(unittest.TestCase):
    def setUp(self):
        SUDDEN_PUMP_CONFIRMATIONS.clear()

    def test_single_five_minute_bar_over_fifteen_percent_is_ignition(self):
        stage = classify_sudden_pump_stage(15.2, 18.0, 1)
        self.assertEqual(stage["stage_key"], "ignition")

    def test_two_hot_bars_accelerate_and_fifty_percent_is_extreme(self):
        by_bars = classify_sudden_pump_stage(21.0, 46.0, 2)
        by_total = classify_sudden_pump_stage(18.0, 51.0, 1)
        self.assertEqual(by_bars["stage_key"], "acceleration")
        self.assertEqual(by_total["stage_key"], "extreme_15m")

    def test_high_confidence_bullish_requires_momentum_volume_and_flow_alignment(self):
        self.assertTrue(sudden_pump_bullish_confirmation(bullish_pump_item()))
        for weaker in (
            {"stage_rank": 1},
            {"volume_ratio": 1.99},
            {"cvd": 0},
            {"oi_change": 0.99},
            {"ratio_change": -0.49},
        ):
            with self.subTest(weaker=weaker):
                self.assertFalse(sudden_pump_bullish_confirmation(bullish_pump_item(**weaker)))

    def test_high_confidence_section_explicitly_says_bullish_without_promising_certain_profit(self):
        section = sudden_pump_push_section(
            bullish_pump_item(),
            "已收盘5MIN确认",
            rendered_at="2026-09-06 09:00:00",
        )
        self.assertIn("看涨 / 急速上涨确认", section)
        self.assertIn("高置信短线看涨", section)
        self.assertIn("高置信不等于必涨", section)

    def test_first_bullish_confirmation_pushes_even_when_phase_was_already_sent(self):
        item = bullish_pump_item()
        episode = item["trigger_bucket"] // 1800 * 1800
        mocked_state = MagicMock()
        mocked_state.query.filter.return_value.all.return_value = [
            SimpleNamespace(signal_key=f"{episode}:acceleration")
        ]
        mocked_db = MagicMock()
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"code": 0}'
        with (
            patch.dict("app.os.environ", {"LARK_THOUGHT_ANALYSIS_WEBHOOK": "https://example.invalid/hook"}),
            patch("app.LarkPushState", mocked_state),
            patch("app.db", mocked_db),
            patch("app.create_push_signal_validation") as mocked_create_validation,
            patch("app.urlopen", return_value=response) as mocked_urlopen,
        ):
            delivered = send_sudden_pump_push([item])
        self.assertTrue(delivered)
        self.assertEqual(
            [call.kwargs["signal_key"] for call in mocked_state.call_args_list],
            [f"{episode}:bullish_confirmed"],
        )
        self.assertIn("高置信短线看涨", mocked_urlopen.call_args.args[0].data.decode("utf-8"))
        mocked_create_validation.assert_called_once_with(
            item, f"{episode}:bullish_confirmed", ANY
        )
        mocked_db.session.commit.assert_called_once()

    @patch("app.time.sleep")
    @patch("app.urlopen", side_effect=OSError("temporary Lark failure"))
    @patch("app.create_push_signal_validation")
    @patch("app.db")
    @patch("app.LarkPushState")
    def test_failed_lark_delivery_retries_without_writing_dedup_state(
        self, mocked_state, mocked_db, mocked_create_validation, mocked_urlopen, mocked_sleep
    ):
        mocked_state.query.filter.return_value.all.return_value = []
        with patch.dict("app.os.environ", {"LARK_THOUGHT_ANALYSIS_WEBHOOK": "https://example.invalid/hook"}):
            delivered = send_sudden_pump_push([bullish_pump_item()])
        self.assertFalse(delivered)
        self.assertEqual(mocked_urlopen.call_count, 3)
        self.assertEqual(mocked_sleep.call_count, 2)
        mocked_db.session.add.assert_not_called()
        mocked_db.session.commit.assert_not_called()
        mocked_create_validation.assert_not_called()

    def test_live_candle_requires_two_separate_confirmations(self):
        self.assertFalse(sudden_pump_confirmation_ready("CYS/USDT", 1000, False, 10))
        self.assertFalse(sudden_pump_confirmation_ready("CYS/USDT", 1000, False, 14))
        self.assertTrue(sudden_pump_confirmation_ready("CYS/USDT", 1000, False, 16))

    def test_closed_candle_is_immediately_confirmed(self):
        self.assertTrue(sudden_pump_confirmation_ready("CYS/USDT", 1000, True, 10))

    @patch("app.get_json")
    def test_cys_like_two_bar_acceleration_is_verified(self, mocked_get_json):
        start = 1_700_000_000_000
        prices = [100, 100, 100, 100, 100, 110, 134.2, 165.1, 166]
        rows = [
            kline(start + index * 300_000, prices[index], prices[index + 1], 1000 + index * 100, 600 + index * 70)
            for index in range(len(prices) - 1)
        ]
        rows.append(kline(start + 8 * 300_000, 166, 220, 5000, 3500))
        mocked_get_json.side_effect = [
            rows,
            [{"sumOpenInterest": value} for value in (100, 103, 107, 112)],
            [{"longShortRatio": value} for value in (1.0, 0.96, 0.91, 0.86)],
        ]
        item = fetch_sudden_pump_context("CYS/USDT", 15.0)
        self.assertIsNotNone(item)
        self.assertGreater(item["change_5m"], 15)
        self.assertGreaterEqual(item["hot_bar_count"], 2)
        self.assertGreater(item["change_15m"], 50)
        self.assertEqual(item["stage_key"], "extreme_15m")
        self.assertGreater(item["oi_change"], 0)
        self.assertLess(item["ratio_change"], 0)

    def test_push_validation_starts_with_first_full_post_push_candle(self):
        self.assertEqual(push_signal_validation_bounds(1001, 10), (1200, 1800))
        self.assertEqual(push_signal_validation_bounds(1200, 10), (1500, 2100))

    def test_push_validation_records_first_target_and_path_metrics(self):
        row = validation_row()
        candles = [
            path_kline(900, 150, 50),  # 推送前极值必须忽略
            path_kline(1200, 106, 99, 105),
            path_kline(1500, 108, 98, 107),
        ]
        status = evaluate_push_signal_validation(row, candles, now_epoch=1801)
        self.assertEqual(status, "win")
        self.assertEqual(row.first_hit, "target")
        self.assertAlmostEqual(row.max_favorable_pct, 8.0)
        self.assertAlmostEqual(row.max_adverse_pct, -2.0)
        self.assertEqual(row.observed_candle_count, 2)

    def test_push_validation_records_first_failure(self):
        row = validation_row()
        status = evaluate_push_signal_validation(
            row,
            [path_kline(1200, 101, 94), path_kline(1500, 108, 98)],
            now_epoch=1801,
        )
        self.assertEqual(status, "loss")
        self.assertEqual(row.first_hit, "failure")

    def test_neutral_and_same_candle_ambiguous_do_not_become_wins(self):
        neutral = validation_row()
        ambiguous = validation_row()
        self.assertEqual(evaluate_push_signal_validation(
            neutral, [path_kline(1200, 103, 98), path_kline(1500, 104, 97)], now_epoch=1801
        ), "neutral")
        self.assertEqual(evaluate_push_signal_validation(
            ambiguous, [path_kline(1200, 106, 94), path_kline(1500, 102, 99)], now_epoch=1801
        ), "insufficient_data")
        self.assertEqual(ambiguous.first_hit, "ambiguous")

    def test_missing_candles_wait_for_grace_then_become_insufficient(self):
        row = validation_row()
        only_one = [path_kline(1200, 106, 99)]
        self.assertEqual(evaluate_push_signal_validation(row, only_one, now_epoch=1801), "pending")
        self.assertEqual(evaluate_push_signal_validation(row, only_one, now_epoch=2401), "insufficient_data")

    def test_category_win_rate_excludes_neutral_pending_and_insufficient(self):
        rows = [
            SimpleNamespace(category="sudden_pump", category_label="5MIN急涨提醒", subtype="ignition", subtype_label="5MIN突然点火", status=status)
            for status in ("win", "win", "loss", "neutral", "pending", "insufficient_data")
        ]
        summary = push_signal_validation_summary(rows)[0]
        self.assertEqual(summary["decided"], 3)
        self.assertAlmostEqual(summary["win_rate"], 200 / 3)
        self.assertEqual(summary["neutral"], 1)
        self.assertEqual(summary["insufficient_data"], 1)


if __name__ == "__main__":
    unittest.main()
