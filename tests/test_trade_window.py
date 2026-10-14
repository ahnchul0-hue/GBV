"""
매매 유예 창 테스트 — 목표 시각 뒤 15분 안에는 실패해도 계속 재시도하고, 알림은 절제한다

실행: python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("GBV_LOG_FILE", os.devnull)
os.environ.setdefault("GBV_LOCK_FILE",
                      os.path.join(tempfile.gettempdir(), "gbv-test-bot.lock"))

import main  # noqa: E402


def at(h, m):
    return datetime(2026, 10, 12, h, m)


class TradeWindowTest(unittest.TestCase):
    """2026-10-12: 22:29:28 / 22:30:06 / 22:30:44 / 22:31:23 네 번 실패 후 ±1분 창이 닫혀 포기했다"""

    def test_window_opens_one_minute_early_and_stays_15_minutes(self):
        self.assertFalse(main._in_trade_window("22:30", now=at(22, 28)))
        self.assertTrue(main._in_trade_window("22:30", now=at(22, 29)))    # 예전과 같은 시작
        self.assertTrue(main._in_trade_window("22:30", now=at(22, 30)))
        self.assertTrue(main._in_trade_window("22:30", now=at(22, 33)))    # 예전에는 여기서 끝
        self.assertTrue(main._in_trade_window("22:30", now=at(22, 45)))
        self.assertFalse(main._in_trade_window("22:30", now=at(22, 46)))

    def test_window_wraps_past_midnight(self):
        self.assertTrue(main._in_trade_window("23:55", now=datetime(2026, 10, 13, 0, 5)))
        self.assertFalse(main._in_trade_window("23:55", now=datetime(2026, 10, 13, 0, 11)))

    def test_blank_or_broken_time_never_opens(self):
        for v in ("", None, "abc", "25:00"):
            with self.subTest(v=v):
                self.assertFalse(main._in_trade_window(v, now=at(22, 30)))
                self.assertFalse(main._after_trade_window(v, now=at(23, 0)))

    def test_after_window_is_same_day_only(self):
        self.assertFalse(main._after_trade_window("22:30", now=at(21, 0)))    # 아직 전
        self.assertFalse(main._after_trade_window("22:30", now=at(22, 40)))   # 창 안
        self.assertTrue(main._after_trade_window("22:30", now=at(22, 46)))
        self.assertTrue(main._after_trade_window("22:30", now=at(23, 59)))

    def test_grace_is_shorter_than_fill_check(self):
        """재시도 창이 미체결 확인(+30분)보다 길면 확인이 주문보다 먼저 돈다"""
        self.assertLess(main.TRADE_GRACE_MIN, main.FILL_CHECK_MIN)


class TradeIfDueTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import trade_state
        p = mock.patch.object(trade_state, "STATE_FILE", os.path.join(self.tmp.name, "trade_state.json"))
        p.start()
        self.addCleanup(p.stop)
        self.notify = mock.patch.object(main, "notify_error").start()
        self.addCleanup(mock.patch.stopall)
        self.fail_notified, self.giveup_notified = {}, {}

    def _run(self, strategy, in_window=True, after=False, is_open=True):
        with mock.patch.object(main, "_in_trade_window", return_value=in_window), \
             mock.patch.object(main, "_after_trade_window", return_value=after):
            main._trade_if_due("us", "미국장", "22:30", is_open, strategy, api=None,
                               today_str="2026-10-12", fail_notified=self.fail_notified,
                               giveup_notified=self.giveup_notified)

    def test_success_marks_traded_and_stays_quiet(self):
        import trade_state
        self._run(mock.Mock())
        self.assertTrue(trade_state.traded_today("us"))
        self.notify.assert_not_called()

    def test_failure_retries_but_alerts_once(self):
        """10-12 처럼 네 번 실패해도 알림은 한 번, 플래그는 안 찍힌다 (다음 루프에 재시도)"""
        import trade_state
        boom = mock.Mock(side_effect=RuntimeError("RC9002 지연"))
        for _ in range(4):
            self._run(boom)
        self.assertEqual(boom.call_count, 4)
        self.assertFalse(trade_state.traded_today("us"))
        self.assertEqual(self.notify.call_count, 1)
        self.assertIn("다시 시도", self.notify.call_args[0][0])

    def test_recovery_after_failures_trades_and_marks(self):
        import trade_state
        flaky = mock.Mock(side_effect=[RuntimeError("지연"), RuntimeError("지연"), None])
        for _ in range(3):
            self._run(flaky)
        self.assertTrue(trade_state.traded_today("us"))
        self.assertEqual(self.notify.call_count, 1)          # 실패 알림 1건, 성공은 조용

    def test_give_up_notice_once_after_window_closes(self):
        self._run(mock.Mock(side_effect=RuntimeError("지연")))          # 창 안 실패
        self._run(mock.Mock(), in_window=False, after=True)              # 창 닫힘
        self._run(mock.Mock(), in_window=False, after=True)              # 또 닫힘
        texts = [c[0][0] for c in self.notify.call_args_list]
        self.assertEqual(len(texts), 2)
        self.assertIn("하지 못했습니다", texts[1])

    def test_no_give_up_notice_without_a_failure(self):
        """시도한 적이 없으면(휴장·아직 전) 포기 알림도 없다"""
        self._run(mock.Mock(), in_window=False, after=True)
        self.notify.assert_not_called()

    def test_closed_market_or_already_traded_does_nothing(self):
        import trade_state
        strategy = mock.Mock()
        self._run(strategy, is_open=False)
        trade_state.mark_traded("us")
        self._run(strategy)
        strategy.assert_not_called()
