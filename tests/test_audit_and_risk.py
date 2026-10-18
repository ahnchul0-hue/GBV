"""
weekly_audit.py (네트워크 없는 부분) · risk_table.py 테스트

실행: python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("GBV_LOG_FILE", os.devnull)
os.environ.setdefault("GBV_LOCK_FILE",
                      os.path.join(tempfile.gettempdir(), "gbv-test-bot.lock"))

import risk_table  # noqa: E402
import weekly_audit as wa  # noqa: E402


# ─────────────────────────────────────────
# .env 로더
# ─────────────────────────────────────────

class LoadEnvTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, ".env")
        for k in ("T_PLAIN", "T_QUOTED", "T_SPACED", "T_BOM", "T_EXISTING"):
            os.environ.pop(k, None)
            self.addCleanup(os.environ.pop, k, None)

    def _write(self, data: bytes):
        with open(self.path, "wb") as f:
            f.write(data)

    def test_plain_quoted_and_spaced_values(self):
        self._write(b"# comment\nT_PLAIN=abc\nT_QUOTED=\"q u o\"\nT_SPACED = spaced \n\n")
        got = wa.load_env(self.path)
        self.assertEqual(got["T_PLAIN"], "abc")
        self.assertEqual(got["T_QUOTED"], "q u o")
        self.assertEqual(got["T_SPACED"], "spaced")
        self.assertEqual(os.environ["T_PLAIN"], "abc")

    def test_bom_from_notepad_is_stripped(self):
        """메모장 UTF-8 저장은 BOM 을 붙인다. 첫 키 이름이 깨지면 안 된다"""
        self._write(bytes([0xEF, 0xBB, 0xBF]) + b"T_BOM=1\r\n")
        self.assertEqual(wa.load_env(self.path).get("T_BOM"), "1")

    def test_existing_environment_wins(self):
        os.environ["T_EXISTING"] = "from-shell"
        self._write(b"T_EXISTING=from-file\n")
        wa.load_env(self.path)
        self.assertEqual(os.environ["T_EXISTING"], "from-shell")

    def test_missing_file_is_fine(self):
        self.assertEqual(wa.load_env(os.path.join(self.tmp.name, "nope")), {})


# ─────────────────────────────────────────
# 자료 수집
# ─────────────────────────────────────────

class CollectTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        logs = os.path.join(self.tmp.name, "logs")
        reports = os.path.join(self.tmp.name, "reports")
        os.makedirs(logs)
        os.makedirs(reports)
        for name, value in (("LOG_DIR", logs), ("REPORT_DIR", reports),
                            ("STATE_FILE", os.path.join(self.tmp.name, "trade_state.json"))):
            p = mock.patch.object(wa, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.logs, self.reports = logs, reports

    def _log(self, stamp, lines):
        with open(os.path.join(self.logs, f"trade_{stamp}.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def test_logs_keep_errors_and_trade_lines_and_drop_noise(self):
        today = date(2026, 10, 14)
        self._log("20261013", [
            "2026-10-13 22:29:04,857 [INFO] [948] __main__ - 미국장 매매 시간 도달: 22:30",
            "2026-10-13 22:29:05,337 [INFO] [948] strategy - TQQQ 현재가: $81.44",
            "2026-10-13 22:29:08,258 [INFO] [948] strategy - GBV매수: TQQQ 1주 @ 81.44",
            "2026-10-13 23:30:48,050 [ERROR] [948] telegram_handler - 폴링 오류: Read timed out",
            "2026-10-13 23:31:03,106 [WARNING] [948] telegram_handler - 텔레그램 폴링이 중단되어 1초 뒤 재시도합니다",
        ])
        self._log("20261001", ["2026-10-01 08:29:00,000 [INFO] [1] __main__ - 생존 신호 전송"])   # 기간 밖
        out = wa.collect_logs(7, today)
        self.assertIn("## trade_20261013.log", out)
        self.assertIn("ERROR 1", out)
        self.assertIn("GBV매수: TQQQ 1주", out)
        self.assertIn("폴링 오류", out)
        self.assertNotIn("현재가: $81.44", out)          # 매매 줄이 아닌 INFO 는 뺀다
        self.assertNotIn("trade_20261001", out)          # 7일 밖
        self.assertNotIn("[948]", out)                   # PID 제거

    def test_reports_skip_separator_rows_and_cap_rows(self):
        with open(os.path.join(self.reports, "미국장.csv"), "w", newline="", encoding="utf-8-sig") as f:
            f.write("기록시각,총자산\n")
            for i in range(50):
                f.write(f"2026-10-{i % 28 + 1:02d},{100000 + i}\n")
                f.write("──────────,\n")
        out = wa.collect_reports(max_rows=5)
        self.assertIn("최근 5행 / 전체 50행", out)
        self.assertNotIn("──────────", out)
        self.assertIn("100049", out)                      # 마지막 행이 들어간다
        self.assertNotIn("100000,", out)                  # 첫 행은 잘린다

    def test_context_is_redacted_and_bounded(self):
        self._log("20261014", [
            "2026-10-14 00:00:00,000 [ERROR] [1] x - url /bot123:AAHsXMEdT3uHhamZ8EbZkYDZCoH1WNDu5rE/getUpdates",
            "2026-10-14 00:00:01,000 [ERROR] [1] x - key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        ])
        with mock.patch.object(wa, "MAX_CONTEXT", 2000), \
             mock.patch.object(wa, "collect_state_and_config", return_value="## config\n" + "x" * 5000):
            out = wa.collect_context(7, date(2026, 10, 14))
        self.assertNotIn("AAHsXMEdT3uHhamZ8EbZkYDZCoH1WNDu5rE", out)
        self.assertNotIn("sk-ant-api03-AbCdEf", out)
        self.assertLessEqual(len(out), 2000 + 40)
        self.assertIn("(중간 생략)", out)


class ArchiveAndCommitsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(wa, "REPORT_DIR", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)

    def _csv(self, name, header, rows):
        with open(os.path.join(self.tmp.name, name), "w", newline="", encoding="utf-8-sig") as f:
            f.write(header + chr(10) + chr(10).join(rows) + chr(10))

    def test_archived_reports_are_included_in_order(self):
        """컬럼이 바뀌어 보관본으로 밀려난 거래가 감사 자료에서 사라지면 안 된다"""
        self._csv("미국장_20261014_223025.csv", "기록시각,총자산", ["2026-10-13 22:29,100000"])
        self._csv("미국장.csv", "기록시각,총자산,실효체결가", ["2026-10-14 22:30,99745,77.13"])
        out = wa.collect_reports()
        self.assertLess(out.index("미국장_20261014_223025.csv (보관본"), out.index("미국장.csv (현재"))
        self.assertIn("2026-10-13 22:29,100000", out)
        self.assertIn("컬럼 설명", out)
        self.assertIn("+면 불리", out)

    def test_no_reports_at_all(self):
        self.assertEqual(wa.collect_reports(), "(리포트 없음)")

    def test_recent_commits_present_and_graceful(self):
        fake = mock.Mock(stdout=("2026-10-14 Keep retrying a failed session" + chr(10)).encode("utf-8"))
        with mock.patch.object(wa.subprocess, "run", return_value=fake):
            out = wa.collect_recent_commits(7)
        self.assertIn("최근 코드 변경", out)
        self.assertIn("Keep retrying", out)
        with mock.patch.object(wa.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertEqual(wa.collect_recent_commits(7), "")

    def test_prompt_states_slippage_convention(self):
        self.assertIn("비용 기준", wa.SYSTEM_PROMPT)
        self.assertIn("조치됨", wa.SYSTEM_PROMPT)


class SendResultTest(unittest.TestCase):

    def test_telegram_length_is_capped(self):
        with mock.patch("notifier._send") as send:
            wa.send_result("가" * 5000, 7, to_stdout=False)
        text = send.call_args[0][0]
        self.assertLessEqual(len(text), wa.TELEGRAM_LIMIT)
        self.assertTrue(text.startswith("🔍 [GBV] 주간 감사"))
        self.assertTrue(text.endswith("…"))

    def test_stdout_mode_does_not_touch_telegram(self):
        with mock.patch("notifier._send") as send, mock.patch("builtins.print") as out:
            wa.send_result("이상 없음", 7, to_stdout=True)
        send.assert_not_called()
        out.assert_called_once()


class RunAuditTest(unittest.TestCase):
    """SDK 응답 모양만 흉내 낸다 (네트워크 없음)"""

    def _client(self, blocks, stop_reason="end_turn"):
        msg = mock.Mock()
        msg.stop_reason = stop_reason
        msg.content = blocks
        msg.model = "claude-opus-5"
        msg.usage = mock.Mock(input_tokens=10, output_tokens=5, cache_read_input_tokens=0)
        msg.stop_details = mock.Mock(category="cyber")
        stream = mock.MagicMock()
        stream.__enter__.return_value.get_final_message.return_value = msg
        client = mock.Mock()
        client.beta.messages.stream.return_value = stream
        return client

    def test_text_blocks_are_joined(self):
        blocks = [mock.Mock(type="thinking", thinking=""), mock.Mock(type="text", text="1. 세션 누락: 없음"),
                  mock.Mock(type="text", text="요약: 정상")]
        self.assertEqual(wa.run_audit(self._client(blocks), "ctx"), "1. 세션 누락: 없음\n요약: 정상")

    def test_refusal_is_reported_not_crashed(self):
        out = wa.run_audit(self._client([], stop_reason="refusal"), "ctx")
        self.assertIn("거부", out)

    def test_request_shape(self):
        """고정 프롬프트는 캐시 표시, 대체 모델 폴백은 켜 둔다"""
        client = self._client([mock.Mock(type="text", text="ok")])
        wa.run_audit(client, "자료", model="claude-opus-5")
        kw = client.beta.messages.stream.call_args.kwargs
        self.assertEqual(kw["model"], "claude-opus-5")
        self.assertEqual(kw["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", kw["betas"])
        self.assertEqual(kw["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(kw["messages"][0]["content"], "자료")
        self.assertNotIn("thinking", kw)        # Opus 5 는 생략하면 adaptive


# ─────────────────────────────────────────
# risk_table
# ─────────────────────────────────────────

class RiskTableTest(unittest.TestCase):

    def test_nearest_row(self):
        key, row = risk_table.nearest(risk_table.TQQQ_2022, 0.33)
        self.assertEqual(key, 0.35)
        key, _ = risk_table.nearest(risk_table.TQQQ_2022, 0.90)   # 표 밖 → 끝 행
        self.assertEqual(key, 0.70)

    def test_latest_total_assets_reads_last_filled_row(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "미국장.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            f.write("기록시각,현금잔고,총자산\n")
            f.write("2026-10-07,\"90,081.84\",\"100,453.53\"\n")
            f.write(",,\n")
            f.write("──────────,,\n")
            f.write("2026-10-09,\"89,834.88\",\"100,177.76\"\n")
            f.write("──────────,,\n")
        with mock.patch.object(risk_table, "REPORT_DIR", tmp.name):
            self.assertAlmostEqual(risk_table.latest_total_assets("미국장"), 100_177.76)
            self.assertEqual(risk_table.latest_total_assets("국내장"), 0.0)

    def test_note_for_tqqq_mentions_mdd_and_cliff(self):
        note = risk_table.risk_note("tqqq", 45_000, capital=100_000)
        self.assertIn("45%", note)
        self.assertIn("MDD -64.0%", note)
        self.assertIn("현금 고갈", note)
        safe = risk_table.risk_note("TQQQ", 30_000, capital=100_000)
        self.assertIn("MDD -41.0%", safe)
        self.assertNotIn("현금 고갈", safe)

    def test_kr_one_x_etf_uses_kr_table_and_leveraged_gets_nothing(self):
        self.assertIn("MDD -9.8%", risk_table.risk_note("360750", 50_000_000, capital=100_000_000))
        self.assertIn("MDD -9.8%", risk_table.risk_note("A360750", 50_000_000, capital=100_000_000))
        self.assertEqual(risk_table.risk_note("418660", 50_000_000, capital=100_000_000), "")

    def test_unknown_capital_gives_empty_note(self):
        with mock.patch.object(risk_table, "latest_total_assets", return_value=0.0):
            self.assertEqual(risk_table.risk_note("TQQQ", 30_000), "")
