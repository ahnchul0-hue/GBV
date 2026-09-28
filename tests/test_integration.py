"""
config_manager / broker / strategy 연동 테스트 (네트워크 없음)

실행: python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config_manager  # noqa: E402
import strategy  # noqa: E402
from broker import validate_api_info  # noqa: E402

SAMPLE_CONFIG = """\
broker      = kiwoom
kiwoom_mode = demo
app_key    = KEY
app_secret = SECRET
account_no =
us_market_time = 19:30
TQQQ = 10000
TQQQ_monthly_rate = 0.02
A252670 = 1000000
trading_enabled = true
"""


class ConfigTest(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(SAMPLE_CONFIG)
        patcher = mock.patch.object(config_manager, "CONFIG_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(os.remove, self.path)

    def test_broker_keys_are_not_tickers(self):
        config = config_manager.load_config()
        self.assertEqual(config_manager.get_broker_name(config), "kiwoom")
        self.assertEqual(config_manager.get_kiwoom_mode(config), "demo")
        self.assertEqual(config_manager.get_all_us_tickers(config), {"TQQQ": 10000.0})
        self.assertEqual(config_manager.get_all_kr_tickers(config), {"A252670": 1000000.0})

    def test_defaults(self):
        self.assertEqual(config_manager.get_broker_name({}), "kiwoom")
        self.assertEqual(config_manager.get_kiwoom_mode({}), "real")

    def test_normalize_kr_ticker(self):
        self.assertEqual(config_manager.normalize_kr_ticker("A252670"), "252670")
        self.assertEqual(config_manager.normalize_kr_ticker("252670"), "252670")
        self.assertEqual(config_manager.normalize_kr_ticker("TQQQ"), "TQQQ")


class ValidateTest(unittest.TestCase):

    def test_kiwoom_does_not_need_account_no(self):
        self.assertEqual(validate_api_info({"APP_KEY": "k", "APP_SECRET": "s"}), "")

    def test_kis_needs_account_no(self):
        self.assertIn("account_no", validate_api_info({"BROKER": "kis", "APP_KEY": "k", "APP_SECRET": "s"}))

    def test_missing_keys(self):
        self.assertIn("app_key", validate_api_info({}))

    def test_unknown_broker(self):
        self.assertIn("broker", validate_api_info({"BROKER": "toss", "APP_KEY": "k", "APP_SECRET": "s"}))


class FakeBroker:
    def __init__(self, kr_holdings, fail_balance_after_order=False):
        self.kr_holdings = kr_holdings
        self.fail_balance_after_order = fail_balance_after_order
        self.orders = []

    def get_kr_balance(self):
        if self.fail_balance_after_order and self.orders:
            raise TimeoutError("잔고 조회 타임아웃")
        return dict(self.kr_holdings), 0

    def get_kr_price(self, ticker):
        return 10000

    def buy_kr(self, ticker, qty, price=0):
        self.orders.append(("buy", ticker, qty, price))
        return True

    def sell_kr(self, ticker, qty, price=0):
        self.orders.append(("sell", ticker, qty, price))
        return True


class StrategyTest(unittest.TestCase):

    def setUp(self):
        for name in ("notify_buy", "notify_sell", "notify_error", "notify_cycle_complete",
                     "save_report", "handle_monthly_increase"):
            patcher = mock.patch.object(strategy, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(strategy.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_prefixed_kr_ticker_matches_holdings(self):
        # config 'A252670', 잔고 키 '252670' → 보유 100주 인식 → 기준금 초과분만 매도
        broker = FakeBroker({"252670": {"qty": 100, "avg_price": 10000.0}})
        trades, _, _ = strategy.execute_gbv(broker, "A252670", 10000, 800000, 0, is_us=False)
        self.assertEqual(broker.orders, [("sell", "A252670", 20, 10000)])
        self.assertEqual(trades[0]["qty"], 20)

    def test_post_order_refresh_failure_keeps_trades(self):
        broker = FakeBroker({"252670": {"qty": 100, "avg_price": 10000.0}}, fail_balance_after_order=True)
        trades, holdings, _ = strategy.execute_gbv(broker, "252670", 10000, 800000, 0, is_us=False)
        self.assertEqual(len(trades), 1)
        self.assertEqual(holdings["252670"]["qty"], 100)   # 매매 전 잔고로 대체

    def _run_kr_cycle(self, broker):
        config = {"A252670": "800000"}
        with mock.patch.object(strategy, "load_config", return_value=config), \
             mock.patch.object(strategy, "get_all_kr_tickers", return_value={"A252670": 800000.0}), \
             mock.patch.object(strategy, "get_base_value", return_value=800000.0):
            strategy.run_kr_strategy(broker)

    def test_failure_after_order_does_not_propagate(self):
        # 주문 후 오류가 main까지 올라가면 매매 시간 창 안에서 같은 주문이 다시 나간다
        broker = FakeBroker({"252670": {"qty": 100, "avg_price": 10000.0}}, fail_balance_after_order=True)
        self._run_kr_cycle(broker)   # 예외 없이 종료
        self.assertEqual(len(broker.orders), 1)
        strategy.notify_error.assert_called_once()
        self.assertIn("재시도하지 않습니다", strategy.notify_error.call_args[0][0])

    def test_failure_before_order_propagates_for_retry(self):
        broker = FakeBroker({})
        with mock.patch.object(broker, "get_kr_balance", side_effect=TimeoutError("타임아웃")):
            with self.assertRaises(TimeoutError):
                self._run_kr_cycle(broker)
        self.assertEqual(broker.orders, [])


if __name__ == "__main__":
    unittest.main()
