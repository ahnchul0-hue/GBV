"""
매매 시각 시간대 지원 · /set 큰 변경 확인 · 실효 체결가 기록 테스트 (네트워크 없음)

실행: python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("GBV_LOG_FILE", os.devnull)
os.environ.setdefault("GBV_LOCK_FILE",
                      os.path.join(tempfile.gettempdir(), "gbv-test-bot.lock"))

import config_manager as cm  # noqa: E402


# ─────────────────────────────────────────
# 1. 'HH:MM ET' 파싱·환산
# ─────────────────────────────────────────

class MarketTimeZoneTest(unittest.TestCase):
    """2026-11-01 에 미국 서머타임이 끝난다. 22:30 KST 고정이면 그날부터 같은 시각이
    정규장 개장(09:30 ET)이 아니라 프리마켓(08:30 ET)이 된다. '09:30 ET' 로 적으면
    봇이 날짜마다 한국 시각으로 환산한다.
    """

    def test_et_resolves_differently_before_and_after_dst_end(self):
        self.assertEqual(cm.resolve_market_time("09:30 ET", date(2026, 10, 30)), "22:30")   # EDT
        self.assertEqual(cm.resolve_market_time("09:30 ET", date(2026, 11, 2)), "23:30")    # EST

    def test_kst_string_passes_through(self):
        self.assertEqual(cm.resolve_market_time("22:30", date(2026, 11, 2)), "22:30")
        self.assertEqual(cm.resolve_market_time("22:30 KST", date(2026, 11, 2)), "22:30")

    def test_raw_keeps_the_zone_and_resolved_is_kst(self):
        cfg = {"US_MARKET_TIME": "09:30 ET"}
        self.assertEqual(cm.get_market_times_raw(cfg), ("09:30 ET", ""))
        us, _ = cm.get_market_times(cfg)
        self.assertRegex(us, r"^2[23]:30$")          # 오늘이 여름이면 22:30, 겨울이면 23:30

    def test_time_crossing_korean_midnight_is_rejected(self):
        """market_calendar 가 한국 날짜와 미국 날짜를 같다고 본다"""
        for v in ("16:00 ET", "19:00 ET", "10:30 ET"):   # 10:30 ET 는 겨울에 00:30 KST
            with self.subTest(v=v):
                with self.assertRaises(ValueError):
                    cm.validate_market_time(v)
                with self.assertLogs("config_manager", level="WARNING"):
                    self.assertEqual(cm.get_market_times({"US_MARKET_TIME": v}), ("", ""))

    def test_unknown_zone_is_rejected(self):
        with self.assertRaises(ValueError):
            cm.validate_market_time("09:30 PST")

    def test_validate_normalises(self):
        self.assertEqual(cm.validate_market_time("9:5 et"), "09:05 ET")
        self.assertEqual(cm.validate_market_time("22:30"), "22:30")
        self.assertEqual(cm.validate_market_time("off"), "")

    def test_describe(self):
        self.assertEqual(cm.describe_market_time(""), "미설정 → 매매 안 함")
        self.assertEqual(cm.describe_market_time("22:30"), "22:30")
        self.assertEqual(cm.describe_market_time("09:30 ET", date(2026, 11, 2)),
                         "09:30 ET (오늘 23:30 KST)")


class NextTradeTimeAcrossDstTest(unittest.TestCase):

    def test_health_next_time_shifts_with_dst(self):
        import bot_status
        # 10-30(금) 23:00 KST: 오늘 22:30 은 지났으니 다음 거래일 11-02(월) 23:30
        now = datetime(2026, 10, 30, 23, 0)
        nxt = bot_status.next_trade_time("us", "09:30 ET", traded_today=True, now=now)
        self.assertEqual(nxt, datetime(2026, 11, 2, 23, 30))
        # 10-29(목) 아침: 오늘 22:30
        now = datetime(2026, 10, 29, 8, 0)
        nxt = bot_status.next_trade_time("us", "09:30 ET", traded_today=False, now=now)
        self.assertEqual(nxt, datetime(2026, 10, 29, 22, 30))

    def test_blank_time_gives_none(self):
        import bot_status
        self.assertIsNone(bot_status.next_trade_time("us", "", False, datetime(2026, 10, 29)))


# ─────────────────────────────────────────
# 2. /set 큰 변경 확인
# ─────────────────────────────────────────

SAMPLE = (
    "kiwoom_mode = demo\n"
    "app_key    = KEY\n"
    "app_secret = SECRET\n"
    "us_market_time = 22:30\n"
    "tqqq = 10000\n"
    "tqqq_monthly_rate = 0.02\n"
    "outside_tqqq = 100\n"
    "trading_enabled = true\n"
)


class FakeBot:
    def __init__(self):
        self.handlers, self.replies = {}, []

    def message_handler(self, commands=None, func=None, **kw):
        def deco(fn):
            for c in commands or []:
                self.handlers[c] = fn
            return fn
        return deco

    def reply_to(self, message, text):
        self.replies.append(text)


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.chat = type("C", (), {"id": 123})()


class SetGuardTest(unittest.TestCase):

    def setUp(self):
        import telegram_handler
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg_path = os.path.join(tmp.name, "config.txt")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write(SAMPLE)
        p = mock.patch.object(cm, "CONFIG_PATH", self.cfg_path)
        p.start()
        self.addCleanup(p.stop)
        self.bot = FakeBot()
        telegram_handler.setup_handlers(self.bot)

    def send(self, text):
        self.bot.replies.clear()
        self.bot.handlers["set"](FakeMessage(text))
        return self.bot.replies[-1] if self.bot.replies else ""

    def value(self, key):
        return cm.load_config().get(key)

    def test_tenfold_cut_requires_confirm(self):
        """0 하나 빠진 오타: $10,000 → $1,000 이면 보유분 90% 매도"""
        reply = self.send("/set tqqq 1000")
        self.assertIn("큰 변경", reply)
        self.assertIn("/set tqqq 1000 confirm", reply)
        self.assertEqual(self.value("TQQQ"), "10000")          # 적용되지 않았다

    def test_confirm_applies(self):
        reply = self.send("/set tqqq 1000 confirm")
        self.assertTrue(reply.startswith("ok"), reply)
        self.assertEqual(self.value("TQQQ"), "1000")

    def test_small_change_applies_without_confirm(self):
        reply = self.send("/set tqqq 12000")                    # 1.2배
        self.assertTrue(reply.startswith("ok"), reply)
        self.assertEqual(self.value("TQQQ"), "12000")

    def test_exactly_double_requires_confirm(self):
        self.assertIn("큰 변경", self.send("/set tqqq 20000"))

    def test_rate_tenfold_requires_confirm(self):
        self.assertIn("큰 변경", self.send("/set tqqq_monthly_rate 0.2"))
        self.assertEqual(self.value("TQQQ_MONTHLY_RATE"), "0.02")

    def test_three_param_form_is_guarded_too(self):
        self.assertIn("큰 변경", self.send("/set tqqq 100000 0.02"))
        self.assertTrue(self.send("/set tqqq 100000 0.02 confirm").startswith("ok"))
        self.assertEqual(self.value("TQQQ"), "100000")

    def test_outside_tqqq_is_guarded(self):
        self.assertIn("큰 변경", self.send("/set outside_tqqq 1500"))
        self.assertEqual(self.value("OUTSIDE_TQQQ"), "100")

    def test_big_base_change_shows_backtest_note(self):
        """확인 메시지에 그 비중의 2022 하락장 성적을 붙인다 (자본은 리포트에서)"""
        import risk_table
        with mock.patch.object(risk_table, "latest_total_assets", return_value=100_000.0):
            reply = self.send("/set tqqq 45000")
        self.assertIn("큰 변경", reply)
        self.assertIn("MDD -64.0%", reply)
        self.assertIn("현금 고갈", reply)

    def test_note_is_omitted_when_capital_unknown(self):
        import risk_table
        with mock.patch.object(risk_table, "latest_total_assets", return_value=0.0):
            reply = self.send("/set tqqq 45000")
        self.assertIn("큰 변경", reply)
        self.assertNotIn("MDD", reply)

    def test_new_ticker_needs_no_confirm(self):
        """기존 값이 없으면 비교 대상이 없다"""
        self.assertTrue(self.send("/set soxl 5000 0.02").startswith("ok"))

    def test_et_time_with_a_space_is_accepted(self):
        reply = self.send("/set us_market_time 09:30 ET")
        self.assertTrue(reply.startswith("ok"), reply)
        self.assertEqual(self.value("US_MARKET_TIME"), "09:30 ET")

    def test_bad_time_is_rejected(self):
        self.assertIn("error", self.send("/set us_market_time 16:00 ET"))
        self.assertIn("error", self.send("/set us_market_time 25:00"))
        self.assertEqual(self.value("US_MARKET_TIME"), "22:30")

    def test_off_turns_the_market_off(self):
        self.assertIn("꺼짐", self.send("/set kr_market_time off"))


# ─────────────────────────────────────────
# 3. 실효 체결가
# ─────────────────────────────────────────

class FillPriceTest(unittest.TestCase):

    def setUp(self):
        import strategy
        self.attach = strategy._attach_fill_price

    def test_buy_fill_and_slippage(self):
        """129주 매수, 현금 100,000 → 89,819.10 : 실효 78.92 (기준 78.70 → +0.28%)"""
        t = [{"action": "매수", "ticker": "TQQQ", "qty": 129, "price": 78.70}]
        self.attach(t, 100_000.00, 89_819.10, 78.70)
        self.assertAlmostEqual(t[0]["fill_price"], 78.92, places=2)
        self.assertAlmostEqual(t[0]["slippage_pct"], 0.28, places=1)

    def test_sell_slippage_sign_is_cost_positive(self):
        """매도는 싸게 팔렸을 때 양수. 3주 매도 @81.18 기준, 입금 242.79 → 80.93"""
        t = [{"action": "매도", "ticker": "TQQQ", "qty": 3, "price": 81.18}]
        self.attach(t, 79_401.99, 79_644.78, 81.18)
        self.assertAlmostEqual(t[0]["fill_price"], 80.93, places=2)
        self.assertGreater(t[0]["slippage_pct"], 0)          # 기준보다 싸게 팔림 = 비용

    def test_unchanged_cash_records_nothing(self):
        """잔고가 아직 반영되지 않았으면 추측하지 않는다"""
        t = [{"action": "매수", "ticker": "TQQQ", "qty": 10, "price": 80.0}]
        self.attach(t, 1000.0, 1000.0, 80.0)
        self.assertNotIn("fill_price", t[0])

    def test_implausible_fill_is_dropped(self):
        """모의계좌의 유령 입출금이 섞이면 단가가 엉뚱해진다 → 공란"""
        t = [{"action": "매수", "ticker": "TQQQ", "qty": 10, "price": 80.0}]
        with self.assertLogs("strategy", level="WARNING"):
            self.attach(t, 20_000.0, 9_000.0, 80.0)          # 1,100/주
        self.assertNotIn("fill_price", t[0])

    def test_multiple_trades_are_skipped(self):
        t = [{"action": "매수", "ticker": "A", "qty": 1, "price": 10.0},
             {"action": "매수", "ticker": "B", "qty": 1, "price": 10.0}]
        self.attach(t, 100.0, 80.0, 10.0)
        self.assertTrue(all("fill_price" not in x for x in t))


class ReportFillColumnsTest(unittest.TestCase):

    def setUp(self):
        import reporter
        self.rp = reporter
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(reporter, "REPORT_DIR", tmp.name)
        p.start()
        self.addCleanup(p.stop)

    def _rows(self, path):
        import csv
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.reader(f))

    def test_trade_row_carries_fill_and_slippage(self):
        trade = {"action": "매수", "ticker": "TQQQ", "qty": 129, "price": 78.70,
                 "fill_price": 78.92, "slippage_pct": 0.28}
        path = self.rp.save_report("미국장", [trade], {"TQQQ": {"qty": 129, "avg_price": 78.72}},
                                   89_819.10, 99_971.40, 10_200.0, {"TQQQ": 78.70}, "USD")
        rows = self._rows(path)
        cols = dict(zip(self.rp.HEADER, rows[1]))
        self.assertEqual(cols["주문기준가"], "78.70")
        self.assertEqual(cols["실효체결가"], "78.92")
        self.assertEqual(cols["슬리피지"], "+0.28%")
        self.assertEqual({len(r) for r in rows}, {len(self.rp.HEADER)})

    def test_missing_fill_leaves_blank(self):
        trade = {"action": "매수", "ticker": "TQQQ", "qty": 1, "price": 78.70}
        path = self.rp.save_report("미국장", [trade], {}, 1.0, 1.0, 0.0, {}, "USD")
        cols = dict(zip(self.rp.HEADER, self._rows(path)[1]))
        self.assertEqual(cols["실효체결가"], "")
        self.assertEqual(cols["슬리피지"], "")
