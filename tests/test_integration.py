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
    def __init__(self, kr_holdings):
        self.kr_holdings = kr_holdings
        self.orders = []

    def get_kr_balance(self):
        return dict(self.kr_holdings), 0

    def buy_kr(self, ticker, qty, price=0):
        self.orders.append(("buy", ticker, qty, price))
        return True

    def sell_kr(self, ticker, qty, price=0):
        self.orders.append(("sell", ticker, qty, price))
        return True


class StrategyTest(unittest.TestCase):

    def setUp(self):
        for name in ("notify_buy", "notify_sell"):
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


if __name__ == "__main__":
    unittest.main()
