"""
config_manager / broker / strategy 연동 테스트 (네트워크 없음)

실행: python -m unittest discover -s tests -v
"""

import os
import sys
import logging
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# main 을 import 하면 logging.basicConfig 가 실제 매매 로그에 FileHandler 를 붙인다.
# 테스트의 가짜 오류가 운영 로그에 섞이지 않도록 import 전에 경로를 돌려놓는다.
os.environ.setdefault("GBV_LOG_FILE", os.devnull)
os.environ.setdefault("GBV_LOCK_FILE",
                      os.path.join(tempfile.gettempdir(), "gbv-test-bot.lock"))

import config_manager  # noqa: E402
import strategy  # noqa: E402
import trade_state  # noqa: E402
import market_calendar  # noqa: E402
from datetime import date  # noqa: E402
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

    def test_setting_keys_are_not_tickers(self):
        # 이전 설정 파일에 남아 있을 수 있는 broker 키도 종목으로 오인하지 않아야 함
        config = config_manager.load_config()
        self.assertEqual(config_manager.get_kiwoom_mode(config), "demo")
        self.assertEqual(config_manager.get_all_us_tickers(config), {"TQQQ": 10000.0})
        self.assertEqual(config_manager.get_all_kr_tickers(config), {"A252670": 1000000.0})

    def test_defaults(self):
        self.assertEqual(config_manager.get_kiwoom_mode({}), "real")

    def test_normalize_kr_ticker(self):
        self.assertEqual(config_manager.normalize_kr_ticker("A252670"), "252670")
        self.assertEqual(config_manager.normalize_kr_ticker("252670"), "252670")
        self.assertEqual(config_manager.normalize_kr_ticker("TQQQ"), "TQQQ")


class ValidateTest(unittest.TestCase):

    def test_account_no_is_optional(self):
        self.assertEqual(validate_api_info({"APP_KEY": "k", "APP_SECRET": "s"}), "")

    def test_missing_keys(self):
        self.assertIn("app_key", validate_api_info({}))

    def test_invalid_mode(self):
        self.assertIn("kiwoom_mode", validate_api_info({"APP_KEY": "k", "APP_SECRET": "s", "KIWOOM_MODE": "live"}))


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

    def get_us_price(self, ticker):
        raise TimeoutError("시세 조회 타임아웃")

    def buy_kr(self, ticker, qty, price=0):
        self.orders.append(("buy", ticker, qty, price))
        return True

    def sell_kr(self, ticker, qty, price=0):
        self.orders.append(("sell", ticker, qty, price))
        return True


class StrategyTest(unittest.TestCase):

    def setUp(self):
        for name in ("notify_buy", "notify_sell", "notify_error", "notify_order_failed", "notify_cycle_complete",
                     "save_report", "handle_monthly_increase"):
            patcher = mock.patch.object(strategy, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(strategy.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(trade_state, "STATE_FILE", os.path.join(tmp.name, "trade_state.json"))
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

    def test_order_attempt_is_recorded_before_sending(self):
        # 주문 전송 직전에 파일에 기록 → 주문 중 봇이 꺼져도 재시작 후 다시 매매하지 않음
        broker = FakeBroker({"252670": {"qty": 100, "avg_price": 10000.0}})
        seen = []
        broker.sell_kr = lambda *a, **k: seen.append(trade_state.traded_today("kr")) or False
        strategy.execute_gbv(broker, "252670", 10000, 800000, 0, is_us=False)
        self.assertEqual(seen, [True])
        self.assertFalse(trade_state.traded_today("us"))

    def test_no_order_means_no_record(self):
        broker = FakeBroker({"252670": {"qty": 80, "avg_price": 10000.0}})   # 기준금과 같음
        strategy.execute_gbv(broker, "252670", 10000, 800000, 0, is_us=False)
        self.assertEqual(broker.orders, [])
        self.assertFalse(trade_state.traded_today("kr"))

    def test_price_failure_is_notified(self):
        broker = FakeBroker({})
        with mock.patch.object(strategy, "load_config", return_value={"TQQQ": "10000"}), \
             mock.patch.object(strategy, "get_all_us_tickers", return_value={"TQQQ": 10000.0}), \
             mock.patch.object(strategy, "get_outside_tqqq", return_value=0):
            strategy.run_us_strategy(broker)
        strategy.notify_error.assert_called_once()
        message = strategy.notify_error.call_args[0][0]
        self.assertIn("TQQQ", message)
        self.assertIn("건너뜁니다", message)
        self.assertEqual(broker.orders, [])


class TradeStateTest(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "trade_state.json")
        patcher = mock.patch.object(trade_state, "STATE_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mark_and_clear(self):
        self.assertFalse(trade_state.traded_today("us"))
        trade_state.mark_traded("us")
        self.assertTrue(trade_state.traded_today("us"))
        self.assertFalse(trade_state.traded_today("kr"))
        trade_state.clear_today("us")
        self.assertFalse(trade_state.traded_today("us"))

    def test_survives_restart(self):
        # 파일에 남기므로 프로세스를 다시 켜도(모듈 상태와 무관하게) 기억한다
        trade_state.mark_traded("kr")
        with open(self.path, encoding="utf-8") as f:
            self.assertIn("kr", f.read())
        self.assertTrue(trade_state.traded_today("kr"))

    def test_yesterday_record_does_not_block_today(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"us": "2000-01-01"}')
        self.assertFalse(trade_state.traded_today("us"))

    def test_corrupted_file_is_tolerated(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertFalse(trade_state.traded_today("us"))
        trade_state.mark_traded("us")
        self.assertTrue(trade_state.traded_today("us"))

    def test_unknown_market(self):
        with self.assertRaises(ValueError):
            trade_state.mark_traded("jp")


class MonthlyIncreaseTest(unittest.TestCase):
    """국내장·미국장 월 증액 기록이 서로를 막지 않는지"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        self.addCleanup(os.remove, self.path)
        patches = [
            mock.patch.object(config_manager, "CONFIG_PATH", self.path),
            mock.patch.object(strategy, "is_trading_day", return_value=True),
            mock.patch.object(strategy, "notify_monthly_increase"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.this_month = strategy.date.today().strftime("%Y-%m")

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_kr_increase_does_not_block_us(self):
        self._write(
            "TQQQ = 10000\nTQQQ_monthly_rate = 0.02\n"
            "A252670 = 1000000\nA252670_monthly_rate = 0.01\n"
        )
        kr = strategy.handle_monthly_increase(config_manager.load_config(), {"A252670": 1000000.0}, "kr")
        us = strategy.handle_monthly_increase(config_manager.load_config(), {"TQQQ": 10000.0}, "us")
        self.assertEqual(kr["A252670"], (1000000.0, 1010000.0))
        self.assertEqual(us["TQQQ"], (10000.0, 10200.0))
        config = config_manager.load_config()
        self.assertEqual(config["LAST_INCREASED_MONTH_KR"], self.this_month)
        self.assertEqual(config["LAST_INCREASED_MONTH_US"], self.this_month)

    def test_same_market_increases_once_per_month(self):
        self._write("TQQQ = 10000\nTQQQ_monthly_rate = 0.02\n")
        strategy.handle_monthly_increase(config_manager.load_config(), {"TQQQ": 10000.0}, "us")
        again = strategy.handle_monthly_increase(config_manager.load_config(), {"TQQQ": 10000.0}, "us")
        self.assertEqual(again, {})
        self.assertEqual(config_manager.load_config()["TQQQ_CURRENT_BASE"], "10200.00")

    def test_legacy_record_prevents_double_increase_after_upgrade(self):
        # 예전 공용 기록이 이번 달이면, 시장별 기록이 없어도 이번 달은 다시 증액하지 않음
        self._write(f"TQQQ = 10000\nTQQQ_monthly_rate = 0.02\nlast_increased_month = {self.this_month}\n")
        result = strategy.handle_monthly_increase(config_manager.load_config(), {"TQQQ": 10000.0}, "us")
        self.assertEqual(result, {})

    def test_market_keys_are_not_tickers(self):
        self._write(f"TQQQ = 10000\nlast_increased_month_us = {self.this_month}\nlast_increased_month_kr = {self.this_month}\n")
        config = config_manager.load_config()
        self.assertEqual(config_manager.get_all_us_tickers(config), {"TQQQ": 10000.0})
        self.assertEqual(config_manager.get_all_kr_tickers(config), {})

    def test_catch_up_when_first_trading_day_was_missed(self):
        # 첫 거래일에 봇이 꺼져 있었어도, 그달 아직 증액 전이면 다음 거래일에 증액
        self._write("TQQQ = 10000\nTQQQ_monthly_rate = 0.02\nlast_increased_month_us = 2000-01\n")
        result = strategy.handle_monthly_increase(config_manager.load_config(), {"TQQQ": 10000.0}, "us")
        self.assertEqual(result["TQQQ"], (10000.0, 10200.0))

    def test_no_increase_on_market_holiday(self):
        self._write("TQQQ = 10000\nTQQQ_monthly_rate = 0.02\n")
        with mock.patch.object(strategy, "is_trading_day", return_value=False):
            result = strategy.handle_monthly_increase(config_manager.load_config(), {"TQQQ": 10000.0}, "us")
        self.assertEqual(result, {})


class MarketCalendarTest(unittest.TestCase):

    def test_weekend(self):
        self.assertFalse(market_calendar.is_trading_day("us", date(2026, 10, 3)))   # 토요일
        self.assertFalse(market_calendar.is_trading_day("kr", date(2026, 10, 4)))   # 일요일

    def test_korean_exchange_holidays(self):
        self.assertFalse(market_calendar.is_trading_day("kr", date(2026, 9, 25)))   # 추석
        self.assertFalse(market_calendar.is_trading_day("kr", date(2026, 12, 31)))  # 연말 휴장일
        self.assertFalse(market_calendar.is_trading_day("kr", date(2026, 5, 1)))    # 노동절
        self.assertTrue(market_calendar.is_trading_day("us", date(2026, 9, 25)))    # 미국은 개장

    def test_us_exchange_holidays(self):
        self.assertFalse(market_calendar.is_trading_day("us", date(2026, 11, 26)))  # 추수감사절
        self.assertFalse(market_calendar.is_trading_day("us", date(2026, 7, 3)))    # 독립기념일(대체)
        self.assertTrue(market_calendar.is_trading_day("kr", date(2026, 11, 26)))   # 한국은 개장

    def test_holiday_name(self):
        self.assertIn("추석", market_calendar.holiday_name("kr", date(2026, 9, 25)))
        self.assertEqual(market_calendar.holiday_name("us", date(2026, 9, 29)), "")

    def test_unknown_market(self):
        with self.assertRaises(ValueError):
            market_calendar.holiday_name("jp", date(2026, 9, 29))


class SellsFirstTest(unittest.TestCase):

    def test_sell_tickers_come_first_and_keep_config_order(self):
        tickers = {"TQQQ": 10000.0, "UGL": 5000.0, "SOXL": 3000.0}
        prices = {"TQQQ": 50.0, "UGL": 30.0, "SOXL": 20.0}
        holdings = {
            "TQQQ": {"qty": 180},   # 9,000 < 10,000 → 매수
            "UGL": {"qty": 200},    # 6,000 > 5,000 → 매도
            "SOXL": {"qty": 200},   # 4,000 > 3,000 → 매도
        }
        with mock.patch.object(strategy, "get_base_value", side_effect=lambda c, t: tickers[t]):
            order = strategy._sells_first(tickers, prices, holdings, {}, 0, is_us=True)
        self.assertEqual(order, ["UGL", "SOXL", "TQQQ"])

    def test_outside_tqqq_counts_toward_sell_decision(self):
        tickers = {"UGL": 5000.0, "TQQQ": 10000.0}
        prices = {"TQQQ": 50.0, "UGL": 30.0}
        holdings = {"TQQQ": {"qty": 150}, "UGL": {"qty": 100}}   # TQQQ 150+60=210주 → 매도
        with mock.patch.object(strategy, "get_base_value", side_effect=lambda c, t: tickers[t]):
            order = strategy._sells_first(tickers, prices, holdings, {}, 60, is_us=True)
        self.assertEqual(order, ["TQQQ", "UGL"])

    def test_prefixed_kr_ticker(self):
        tickers = {"A252670": 800000.0, "005930": 1000000.0}
        prices = {"A252670": 10000.0, "005930": 70000.0}
        holdings = {"252670": {"qty": 100}, "005930": {"qty": 10}}   # 1,000,000 > 800,000 → 매도
        with mock.patch.object(strategy, "get_base_value", side_effect=lambda c, t: tickers[t]):
            order = strategy._sells_first(tickers, prices, holdings, {}, 0, is_us=False)
        self.assertEqual(order, ["A252670", "005930"])


if __name__ == "__main__":
    unittest.main()


class DryRunTest(unittest.TestCase):

    def test_orders_are_not_sent(self):
        from broker import DryRunBroker
        real = mock.Mock()
        real.get_kr_price.return_value = 10000
        with mock.patch("notifier._send") as send:
            api = DryRunBroker(real)
            self.assertTrue(api.buy_kr("252670", 3, 10000))
            self.assertTrue(api.sell_us("TQQQ", 2))
            self.assertEqual(api.get_kr_price("252670"), 10000)   # 조회는 진짜 API로
        real.buy_kr.assert_not_called()
        real.sell_us.assert_not_called()
        self.assertEqual(send.call_count, 2)
        self.assertIn("DRY-RUN", send.call_args_list[0][0][0])

    def test_create_broker_wraps_when_enabled(self):
        import broker
        config = {"APP_KEY": "k", "APP_SECRET": "s", "KIWOOM_MODE": "demo", "DRY_RUN": "true"}
        with mock.patch("kiwoom_api.KiwoomAPI") as fake_api:   # 토큰 발급(네트워크) 막기
            self.assertIsInstance(broker.create_broker(config), broker.DryRunBroker)
            config["DRY_RUN"] = "false"
            self.assertIs(broker.create_broker(config), fake_api.return_value)

    def test_setting_keys_are_not_tickers(self):
        config = {"DRY_RUN": "1", "TELEGRAM_CHAT_ID": "12345", "TELEGRAM_BOT_TOKEN": "1", "TQQQ": "100"}
        self.assertEqual(config_manager.get_all_us_tickers(config), {"TQQQ": 100.0})
        self.assertEqual(config_manager.get_all_kr_tickers(config), {})


class TelegramOwnerTest(unittest.TestCase):

    def _msg(self, chat_id):
        return mock.Mock(chat=mock.Mock(id=chat_id))

    def test_only_owner_chat_is_accepted(self):
        import telegram_handler
        with mock.patch.object(telegram_handler, "get_telegram_settings", return_value=("t", "12345")):
            self.assertTrue(telegram_handler.is_owner(self._msg(12345)))
            self.assertFalse(telegram_handler.is_owner(self._msg(99999)))

    def test_missing_chat_id_accepts_nobody(self):
        import telegram_handler
        with mock.patch.object(telegram_handler, "get_telegram_settings", return_value=("t", "")):
            self.assertFalse(telegram_handler.is_owner(self._msg(12345)))

    def test_config_overrides_notifier_constants(self):
        import notifier
        with mock.patch("config_manager.load_config",
                        return_value={"TELEGRAM_BOT_TOKEN": "cfg-token", "TELEGRAM_CHAT_ID": " 777 "}):
            self.assertEqual(notifier.get_telegram_settings(), ("cfg-token", "777"))
        with mock.patch("config_manager.load_config", return_value={}):
            self.assertEqual(notifier.get_telegram_settings(),
                             (notifier.TELEGRAM_BOT_TOKEN, notifier.TELEGRAM_CHAT_ID))


class MainLoopHelpersTest(unittest.TestCase):

    def test_target_time_wraps_midnight(self):
        import main
        from datetime import datetime as real_dt
        with mock.patch.object(main, "datetime") as dt:
            dt.now.return_value = real_dt(2026, 9, 29, 0, 20)
            self.assertTrue(main._is_target_time("23:50", offset_min=30))
            self.assertFalse(main._is_target_time("23:50"))
            dt.now.return_value = real_dt(2026, 9, 29, 23, 30)
            self.assertTrue(main._is_target_time("00:30", offset_min=-60))

    def test_unfilled_report(self):
        import main
        api = mock.Mock()
        api.get_us_unfilled.return_value = [
            {"ticker": "TQQQ", "side": "매수", "qty": 5, "remaining": 3, "price": 51.5, "ord_no": "1"}]
        with mock.patch.object(main, "_send") as send:
            main._notify_unfilled(api, "us")
        text = send.call_args[0][0]
        self.assertIn("미체결 주문 1건", text)
        self.assertIn("매수 TQQQ 잔량 3/5주 @ $51.50", text)

    def test_no_unfilled_sends_nothing(self):
        import main
        api = mock.Mock()
        api.get_kr_unfilled.return_value = []
        with mock.patch.object(main, "_send") as send, mock.patch.object(main, "notify_error") as err:
            main._notify_unfilled(api, "kr")
        send.assert_not_called()
        err.assert_not_called()

    def test_unfilled_query_failure_is_reported(self):
        import main
        api = mock.Mock()
        api.get_kr_unfilled.side_effect = TimeoutError("타임아웃")
        with mock.patch.object(main, "notify_error") as err:
            main._notify_unfilled(api, "kr")
        err.assert_called_once()


class CheckSetupTest(unittest.TestCase):

    def test_kiwoom_hint_for_bad_key(self):
        import check_setup
        check_setup.results.clear()
        config = {"APP_KEY": "realkey", "APP_SECRET": "realsecret", "KIWOOM_MODE": "real"}
        err = Exception("키움 API 오류 (3): 인증에 실패했습니다[8001:App Key와 Secret Key 검증에 실패했습니다] (api-id=au10001)")
        with mock.patch("kiwoom_api.KiwoomAPI", side_effect=err), \
             mock.patch("builtins.print") as out:
            self.assertFalse(check_setup.check_kiwoom(config))
        printed = "\n".join(str(c.args[0]) for c in out.call_args_list if c.args)
        self.assertIn("kiwoom_mode = demo", printed)

    def test_placeholder_detection(self):
        import check_setup
        self.assertTrue(check_setup.is_placeholder("XXXXXXXX"))
        self.assertTrue(check_setup.is_placeholder(""))
        self.assertFalse(check_setup.is_placeholder("123456789:ABC"))

    def test_main_stops_cleanly_when_kiwoom_fails(self):
        import main
        with mock.patch.object(main.single_instance, "acquire"), \
             mock.patch.object(main, "load_config", return_value={"APP_KEY": "k", "APP_SECRET": "s"}), \
             mock.patch.object(main, "get_broker", side_effect=RuntimeError("8001")), \
             mock.patch.object(main, "notify_error") as err, \
             mock.patch.object(main.threading, "Thread") as thread:
            main.main()
        err.assert_called_once()
        thread.assert_not_called()


class OrderNotificationTest(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(trade_state, "STATE_FILE", os.path.join(tmp.name, "trade_state.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(strategy.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rejected_order_is_notified_with_reason(self):
        broker = FakeBroker({})
        def reject(ticker, qty, price=0):
            broker.last_order_error = "주문가능금액이 부족합니다"
            return False
        broker.buy_kr = reject
        with mock.patch("notifier._send") as send:
            trades, _, _ = strategy.execute_gbv(broker, "418660", 10000, 1000000, 0, is_us=False)
        self.assertEqual(trades, [])
        text = send.call_args[0][0]
        self.assertIn("매수 주문 실패 418660 99주 @ ₩10,000", text)
        self.assertIn("주문가능금액이 부족합니다", text)

    def test_success_uses_market_currency(self):
        broker = FakeBroker({})
        with mock.patch("notifier._send") as send:
            strategy.execute_gbv(broker, "418660", 10000, 1000000, 0, is_us=False)
        self.assertEqual(send.call_args_list[0][0][0], "✅ 매수 주문 418660 99주 @ ₩10,000")

    def test_kiwoom_order_failure_keeps_reason(self):
        from kiwoom_api import KiwoomAPI
        api = KiwoomAPI.__new__(KiwoomAPI)
        with mock.patch.object(KiwoomAPI, "_us_order", side_effect=RuntimeError("[2000] 잔고 부족")):
            self.assertFalse(api.buy_us("TQQQ", 1))
        self.assertIn("잔고 부족", api.last_order_error)


class BotStatusTest(unittest.TestCase):

    def setUp(self):
        import bot_status
        self.bs = bot_status
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        for target, value in ((trade_state, ("STATE_FILE", os.path.join(tmp.name, "trade_state.json"))),
                              (bot_status, ("LOG_DIR", tmp.name))):
            patcher = mock.patch.object(target, *value)
            patcher.start()
            self.addCleanup(patcher.stop)
        bot_status.clear_errors()

    def test_next_trade_time_skips_holidays_and_done(self):
        from datetime import datetime as dt
        now = dt(2026, 10, 8, 23, 0)   # 목요일 밤, 다음날 10/9 한글날(국내 휴장)
        nxt = self.bs.next_trade_time("kr", "09:00", traded_today=True, now=now)
        self.assertEqual(nxt, dt(2026, 10, 12, 9, 0))           # 금(휴장)·주말 건너뛰고 월요일
        nxt = self.bs.next_trade_time("us", "22:30", traded_today=False, now=dt(2026, 9, 30, 21, 0))
        self.assertEqual(nxt, dt(2026, 9, 30, 22, 30))          # 오늘 아직 안 함
        nxt = self.bs.next_trade_time("us", "22:30", traded_today=False, now=dt(2026, 9, 30, 23, 0))
        self.assertEqual(nxt, dt(2026, 10, 1, 22, 30))          # 시각 지남 → 다음 날

    def test_error_handler_records_last_error(self):
        logger = logging.getLogger("bot_status_test")
        handler = self.bs.LastErrorHandler()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        logger.info("평범한 로그")
        self.assertIsNone(self.bs.last_error())
        logger.error("해외 매수 실패 (TQQQ): 잔고 부족\n상세")
        self.assertEqual(self.bs.last_error()[1], "해외 매수 실패 (TQQQ): 잔고 부족")

    def test_health_text(self):
        from datetime import datetime as dt
        config = {"KIWOOM_MODE": "demo", "US_MARKET_TIME": "22:30", "KR_MARKET_TIME": "09:00",
                  "TQQQ": "10000", "418660": "1000000"}
        with mock.patch("config_manager.load_config", return_value=config), \
             mock.patch.object(trade_state, "date") as d:
            d.today.return_value = date(2026, 9, 30)
            trade_state.mark_traded("kr")
            text = self.bs.health_text(now=dt(2026, 9, 30, 12, 0))
        self.assertIn("모의투자", text)
        self.assertIn("미국장: ⏳ 오늘 아직 / 다음 오늘(수) 22:30", text)
        self.assertIn("국내장: ✅ 오늘 매매함 / 다음 내일(목) 09:00", text)
        self.assertIn("마지막 오류: 없음", text)

    def test_tail_log_shortens_and_filters(self):
        path = os.path.join(self.tmp, "trade_20260930.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("2026-09-30 01:08:08,387 [INFO] __main__ - 매매 대기 중...\n"
                    "2026-09-30 01:08:44,455 [WARNING] kiwoom_api - 달러 조회 실패\n"
                    "2026-09-30 01:09:00,000 [INFO] telegram_handler - /balance 실행\n")
        text = self.bs.tail_log(2)
        self.assertIn("최근 2줄", text)
        self.assertIn("01:08:44 W kiwoom_api 달러 조회 실패", text)
        self.assertNotIn("매매 대기", text)
        self.assertIn("달러 조회 실패", self.bs.tail_log(20, errors_only=True))
        self.assertNotIn("/balance", self.bs.tail_log(20, errors_only=True))

    def test_tail_log_respects_telegram_limit(self):
        path = os.path.join(self.tmp, "trade_20260930.log")
        with open(path, "w", encoding="utf-8") as f:
            for i in range(100):
                f.write(f"2026-09-30 01:00:00,000 [INFO] x - {'가' * 100} {i}\n")
        self.assertLessEqual(len(self.bs.tail_log(100)), 4096)

    def test_heartbeat_time_setting(self):
        self.assertEqual(config_manager.get_heartbeat_time({}), "08:30")
        self.assertEqual(config_manager.get_heartbeat_time({"HEARTBEAT_TIME": "off"}), "")
        self.assertEqual(config_manager.get_heartbeat_time({"HEARTBEAT_TIME": "7:05"}), "07:05")
        self.assertEqual(config_manager.get_heartbeat_time({"HEARTBEAT_TIME": "abc"}), "08:30")

    def test_heartbeat_sends_once_a_day(self):
        import main
        from datetime import datetime as real_dt
        with mock.patch.object(main, "load_config", return_value={"HEARTBEAT_TIME": "08:30"}), \
             mock.patch.object(main, "_send") as send, \
             mock.patch.object(main.bot_status, "health_text", return_value="ok"), \
             mock.patch.object(main, "datetime") as dt:
            dt.now.return_value = real_dt(2026, 10, 3, 8, 30)   # 토요일도 보냄
            sent = main._maybe_send_heartbeat(None)
            sent = main._maybe_send_heartbeat(sent)
        self.assertEqual(sent, "2026-10-03")
        send.assert_called_once_with("ok")


# ─────────────────────────────────────────
# 텔레그램 폴링 종료
# ─────────────────────────────────────────

class TelegramShutdownTest(unittest.TestCase):
    """stop_polling() 이 진행 중인 long-poll 을 깨우는지 확인.

    stop_event 만 쓰던 시절에는 polling() 이 끝날 때까지 스레드가 살아 있어
    main 의 join(timeout) 이 매번 만료됐다.
    """

    def _run(self, use_stop_polling):
        import threading
        import telegram_handler

        in_poll = threading.Event()
        wake    = threading.Event()

        class FakeBot:
            def polling(self, **kwargs):
                in_poll.set()
                wake.wait(10)        # 실제 long-poll 을 흉내
                wake.clear()

            def stop_polling(self):
                wake.set()

        stop_event = threading.Event()
        with mock.patch.object(telegram_handler, "telebot") as tb, \
             mock.patch.object(telegram_handler, "get_telegram_settings",
                               return_value=("TOKEN", "12345")), \
             mock.patch.object(telegram_handler, "setup_handlers"), \
             mock.patch.object(telegram_handler, "_add_stranger_guard"):
            tb.TeleBot.return_value = FakeBot()
            t = threading.Thread(target=telegram_handler.start_polling,
                                 args=(stop_event,), daemon=True)
            t.start()
            self.assertTrue(in_poll.wait(5), "폴링이 시작되지 않음")

            stop_event.set()
            if use_stop_polling:
                telegram_handler.stop_polling()
            t.join(timeout=2)
            alive = t.is_alive()

        wake.set()               # 남은 스레드 정리
        telegram_handler._bot = None
        return alive

    def test_stop_polling_wakes_the_thread(self):
        self.assertFalse(self._run(use_stop_polling=True),
                         "stop_polling() 을 불렀는데도 스레드가 남아 있음")

    def test_stop_event_alone_leaves_thread_blocked(self):
        # 수정 전 동작 재현: 이벤트만 세팅하면 long-poll 이 끝날 때까지 안 멈춘다
        self.assertTrue(self._run(use_stop_polling=False),
                        "stop_event 만으로 멈췄다면 이 테스트의 전제가 틀린 것")

# ─────────────────────────────────────────
# 중복 실행 차단
# ─────────────────────────────────────────

class SingleInstanceTest(unittest.TestCase):
    """OS 파일 잠금이므로 프로세스가 죽으면 OS 가 알아서 풀어 준다.

    윈도우에서는 PID 생사 확인을 쓸 수 없다. os.kill(pid, 0) 이
    TerminateProcess 라 확인하려던 프로세스를 죽여 버린다.
    """

    def setUp(self):
        import single_instance
        self.si = single_instance
        self.si.release()      # 앞 테스트가 잠금을 남겼어도 깨끗한 상태에서 시작
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = os.path.join(self.tmp.name, "bot.lock")
        self.pid  = os.path.join(self.tmp.name, "bot.pid")
        self.addCleanup(self._force_release)

    def _force_release(self):
        try:
            self.si.release()
        except Exception:
            pass

    def _acquire_as_other_process(self):
        """_handle 을 비워 두 번째 acquire 가 실제로 잠금을 다투게 한다.

        잠금은 열린 파일 핸들에 걸리므로, 핸들이 다르면 같은 프로세스 안에서도
        실제 프로세스 두 개와 똑같이 충돌한다.
        """
        held = self.si._handle
        self.si._handle = None
        try:
            self.si.acquire(self.lock, self.pid)
            self.si._handle = held        # 잡혔다면 원복하고 실패로 보고
            return True
        except self.si.AlreadyRunning as e:
            self.si._handle = held
            self.err = e
            return False

    def test_second_instance_is_refused_with_pid(self):
        self.si.acquire(self.lock, self.pid)
        self.assertFalse(self._acquire_as_other_process(), "두 번째 인스턴스가 잠금을 잡았다")
        self.assertIn(str(os.getpid()), str(self.err))

    def test_acquire_is_idempotent_in_one_process(self):
        self.si.acquire(self.lock, self.pid)
        self.si.acquire(self.lock, self.pid)      # 같은 프로세스에서는 무해해야 한다
        self.si.release()

    def test_release_frees_the_lock_and_removes_pid_file(self):
        self.si.acquire(self.lock, self.pid)
        self.assertTrue(os.path.exists(self.pid))
        self.si.release()
        self.assertFalse(os.path.exists(self.pid))
        self.si.acquire(self.lock, self.pid)      # 다시 잡을 수 있어야 한다
        self.si.release()

    def test_release_without_acquire_is_noop(self):
        self.si.release()

    def test_main_refuses_to_start_when_already_running(self):
        import main
        import single_instance
        with mock.patch.object(single_instance, "acquire",
                               side_effect=single_instance.AlreadyRunning("4242")),              mock.patch.object(main, "load_config") as load:
            main.main()
        load.assert_not_called()      # 봇 본체는 시작하지 않아야 한다

# ─────────────────────────────────────────
# /log 포맷
# ─────────────────────────────────────────

class TailLogTest(unittest.TestCase):
    """로그 포맷에 PID 를 넣었을 때 /log 파싱이 조용히 깨진 적이 있다.

    깨져도 예외가 아니라 '보기 흉한 긴 줄'이 나올 뿐이라 눈치채기 어렵다.
    옛 로그(PID 없음)와 새 로그(PID 있음)가 한 파일에 섞이는 것도 정상이다.
    """

    OLD = "2026-09-30 01:08:44,455 [INFO] kiwoom_api - 달러 예수금: $100"
    NEW = "2026-09-30 02:55:37,183 [INFO] [10808] __main__ - 매매 대기 중..."
    ERR = "2026-09-30 02:41:40,440 [ERROR] [7080] single_instance - 이미 실행 중"
    RAW = '    raise TimeoutError("잔고 조회 타임아웃")'

    def _tail(self, rows, **kw):
        import bot_status
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "trade_20260930.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write(chr(10).join(rows) + chr(10))
        with mock.patch.object(bot_status, "LOG_DIR", tmp.name):
            return bot_status.tail_log(**kw)

    def test_pid_is_stripped_and_module_kept(self):
        out = self._tail([self.NEW], lines=1)
        self.assertIn("02:55:37 I __main__ 매매 대기 중...", out)
        self.assertNotIn("10808", out)      # PID 는 파일에만, 텔레그램에는 안 보낸다
        self.assertNotIn(" - ", out)        # 모듈 뒤 구분자도 떼어낸다

    def test_old_format_without_pid_still_parses(self):
        out = self._tail([self.OLD], lines=1)
        self.assertIn("01:08:44 I kiwoom_api 달러 예수금: $100", out)

    def test_unparsable_line_passes_through(self):
        out = self._tail([self.RAW], lines=1)
        self.assertIn(self.RAW, out)

    def test_errors_only_filter(self):
        out = self._tail([self.OLD, self.NEW, self.ERR], lines=10, errors_only=True)
        self.assertIn("single_instance 이미 실행 중", out)
        self.assertNotIn("매매 대기 중", out)



# ─────────────────────────────────────────
# 로그 비밀값 가리기
# ─────────────────────────────────────────

class LogRedactTest(unittest.TestCase):
    """텔레그램 폴링이 실패하면 requests 예외에 요청 URL 이 통째로 들어가고,
    그 URL 경로에 봇 토큰이 들어 있다. 로그 파일에 남고 /log 로 다시 나간다.
    """

    # 2026-09-30 06:41 에 실제로 로그에 찍힌 줄 (토큰만 예시값으로 교체)
    REAL = ("폴링 오류: HTTPSConnectionPool(host='api.telegram.org', port=443): "
            "Max retries exceeded with url: "
            "/bot1234567890:AAHsXMEdT3uHhamZ8EbZkYDZCoH1WNDu5rE/getUpdates"
            "?offset=98990243&timeout=5")
    SECRET = "1234567890:AAHsXMEdT3uHhamZ8EbZkYDZCoH1WNDu5rE"

    def setUp(self):
        import log_redact
        self.lr = log_redact
        kept = list(log_redact._secrets)
        self.addCleanup(lambda: log_redact._secrets.__setitem__(slice(None), kept))

    def test_token_in_url_is_masked(self):
        out = self.lr.redact(self.REAL)
        self.assertNotIn(self.SECRET, out)
        self.assertIn("/bot1234567890:***/getUpdates", out)   # 봇 ID 는 남겨 디버깅에 쓴다
        self.assertIn("offset=98990243", out)                 # 나머지는 건드리지 않는다

    def test_registered_secret_is_masked_outside_urls(self):
        self.lr.add_secret(self.SECRET)
        self.assertNotIn(self.SECRET, self.lr.redact(f"토큰은 {self.SECRET} 입니다"))

    def test_ordinary_line_is_untouched(self):
        line = "달러 예수금(d4_usd_fx_entr): $100,000.00"
        self.assertEqual(self.lr.redact(line), line)

    def test_formatter_masks_traceback_too(self):
        """logger.error(..., exc_info=True) 의 트레이스백은 getMessage() 에 없다."""
        fmt = self.lr.RedactingFormatter("%(message)s")
        try:
            raise RuntimeError(self.REAL)
        except RuntimeError:
            record = logging.LogRecord("t", logging.ERROR, __file__, 1,
                                       "텔레그램 봇 시작 실패", (), sys.exc_info())
        out = fmt.format(record)
        self.assertIn("RuntimeError", out)          # 트레이스백이 실제로 붙었는지 확인
        self.assertNotIn(self.SECRET, out)

    def test_health_last_error_is_masked(self):
        import bot_status
        bot_status.clear_errors()
        self.addCleanup(bot_status.clear_errors)
        bot_status.LastErrorHandler().emit(
            logging.LogRecord("t", logging.ERROR, __file__, 1, self.REAL, (), None))
        when, message = bot_status.last_error()
        self.assertNotIn(self.SECRET, message)
