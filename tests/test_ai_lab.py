"""
AI 실험실 테스트 — rules_lab · shadow_trader · ai_researcher (네트워크 없음)

실행: python -m unittest discover -s tests -v
"""

import csv
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("GBV_LOG_FILE", os.devnull)
os.environ.setdefault("GBV_LOCK_FILE",
                      os.path.join(tempfile.gettempdir(), "gbv-test-bot.lock"))

import pandas as pd  # noqa: E402

import backtest_dual as bd  # noqa: E402
import rules_lab as rl  # noqa: E402
import shadow_trader as st  # noqa: E402
import ai_researcher as ar  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def series(values, start="2022-01-03"):
    idx = pd.bdate_range(start=start, periods=len(values))
    return pd.Series([float(v) for v in values], index=idx)


def crash_and_rebound(n=120):
    """100 → 40 → 110 모양의 합성 가격"""
    import math
    vals = []
    for i in range(n):
        if i < 40:
            vals.append(100 * (1 - 0.6 * i / 40))
        else:
            vals.append(40 + 70 * (i - 40) / (n - 41))
    return series(vals)


# ─────────────────────────────────────────
# 안전벨트
# ─────────────────────────────────────────

class SafetyBeltTest(unittest.TestCase):
    """AI 가 붙은 파일은 주문 경로를 모르고, 주문 경로는 AI 를 모른다"""

    ORDER_MARKS = re.compile(r"buy_us|sell_us|buy_kr|sell_kr|_us_order|_kr_order|ust2000[01]|kt1000[01]"
                             r"|^\s*(import|from)\s+(broker|kiwoom_api)\b", re.M)
    AI_MARKS = re.compile(r"anthropic|claude", re.I)

    def _src(self, name):
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            return f.read()

    def test_ai_files_never_touch_orders(self):
        for name in ("shadow_trader.py", "ai_researcher.py", "rules_lab.py", "ai_client.py", "weekly_audit.py"):
            with self.subTest(name=name):
                self.assertIsNone(self.ORDER_MARKS.search(self._src(name)), f"{name} 에 주문 경로 참조")

    def test_order_path_never_calls_ai(self):
        for name in ("main.py", "strategy.py", "kiwoom_api.py", "broker.py",
                     "telegram_handler.py", "notifier.py", "config_manager.py"):
            with self.subTest(name=name):
                self.assertIsNone(self.AI_MARKS.search(self._src(name)), f"{name} 에 AI 참조")


# ─────────────────────────────────────────
# rules_lab
# ─────────────────────────────────────────

class RuleParityTest(unittest.TestCase):
    """훅을 끄면 backtest_dual.run_bucket 과 센트 단위로 같아야 한다"""

    def _check(self, px, capital=100_000.0, ratio=0.3, rate=0.02, cash_rate=0.0):
        a = bd.run_bucket(px, capital, capital * ratio, rate, "2022-01-01", "2022-12-31",
                          annual_cash_rate=cash_rate)
        b = rl.run_rules(px, capital, rl.RuleParams(base_ratio=ratio, monthly_rate=rate),
                         "2022-01-01", "2022-12-31", annual_cash_rate=cash_rate)
        self.assertEqual(len(a), len(b))
        pd.testing.assert_series_equal(a["total"], b["total"], check_names=False)
        pd.testing.assert_series_equal(a["qty"], b["qty"], check_names=False)

    def test_flat_series(self):
        self._check(series([100] * 60))

    def test_crash_and_rebound(self):
        self._check(crash_and_rebound())

    def test_with_cash_interest_and_high_ratio(self):
        self._check(crash_and_rebound(), ratio=0.6, rate=0.03, cash_rate=0.04)


class RuleHooksTest(unittest.TestCase):

    def test_pause_hook_skips_increase_when_cash_is_thin(self):
        px = series([100] * 70)       # 1월~4월, 거래 없음에 가까움
        on = rl.run_rules(px, 10_000, rl.RuleParams(base_ratio=0.9, monthly_rate=0.05, pause_below_cash=5.0),
                          "2022-01-01", "2022-12-31")
        off = rl.run_rules(px, 10_000, rl.RuleParams(base_ratio=0.9, monthly_rate=0.05),
                           "2022-01-01", "2022-12-31")
        self.assertLess(on["base"].iloc[-1], off["base"].iloc[-1])   # 증액이 건너뛰어졌다

    def test_bump_hook_fires_once_per_drawdown(self):
        px = crash_and_rebound()
        df = rl.run_rules(px, 100_000, rl.RuleParams(base_ratio=0.2, monthly_rate=0.0,
                                                    bump_drawdown=0.3, bump_pct=0.5),
                          "2022-01-01", "2022-12-31")
        bases = df["base"].round(2).unique()
        self.assertEqual(len(bases), 2)                   # 원래 기준금, 한 번 올린 기준금
        self.assertAlmostEqual(bases.max() / bases.min(), 1.5, places=2)

    def test_cap_hook_limits_base(self):
        px = series([100] * 40)
        df = rl.run_rules(px, 10_000, rl.RuleParams(base_ratio=0.9, monthly_rate=0.05, cap_ratio=0.5),
                          "2022-01-01", "2022-12-31")
        off = rl.run_rules(px, 10_000, rl.RuleParams(base_ratio=0.9, monthly_rate=0.05),
                           "2022-01-01", "2022-12-31")
        # 상한은 매매 전 자산 기준이라 체결 수수료만큼 오차가 난다
        self.assertTrue((df["base"] <= 0.5 * df["total"] * (1 + 2 * rl.COMMISSION)).all())
        self.assertLess(df["base"].iloc[-1], off["base"].iloc[-1])


class RuleParamsTest(unittest.TestCase):

    def test_bounds(self):
        rl.RuleParams(base_ratio=0.3, monthly_rate=0.02).validate()
        with self.assertRaises(ValueError):
            rl.RuleParams(base_ratio=0.95, monthly_rate=0.02).validate()
        with self.assertRaises(ValueError):
            rl.RuleParams(base_ratio=0.3, monthly_rate=0.02, bump_drawdown=0.5).validate()   # bump_pct 없음
        with self.assertRaises(ValueError):
            rl.RuleParams.from_dict({"base_ratio": "0.3", "monthly_rate": 0.02})

    def test_from_dict_ignores_unknown_and_label(self):
        p = rl.RuleParams.from_dict({"base_ratio": 0.3, "monthly_rate": 0.02, "foo": 1, "cap_ratio": None})
        self.assertTrue(p.hooks_off())
        self.assertIn("기준금 30%", p.label())


class JudgeTest(unittest.TestCase):

    BASE = {"수익률%": 10.0, "MDD%": -40.0, "자금부족일": 0}

    def test_pass_and_fail_reasons(self):
        ok, why = rl.judge({"수익률%": 12.0, "MDD%": -42.0, "자금부족일": 0}, self.BASE)
        self.assertTrue(ok, why)
        self.assertFalse(rl.judge({"수익률%": 12.0, "MDD%": -46.0, "자금부족일": 0}, self.BASE)[0])   # MDD 6%p 악화
        self.assertFalse(rl.judge({"수익률%": 10.2, "MDD%": -40.0, "자금부족일": 0}, self.BASE)[0])   # +0.2%p 뿐
        self.assertFalse(rl.judge({"수익률%": 30.0, "MDD%": -40.0, "자금부족일": 3}, self.BASE)[0])   # 자금부족

    def test_evaluate_marks_baseline_and_candidates(self):
        px = crash_and_rebound(260)
        per = {"A": ("2022-01-01", "2022-06-30"), "B": ("2022-07-01", "2022-12-31")}
        base = rl.RuleParams(base_ratio=0.1, monthly_rate=0.02)
        cands = [rl.RuleParams(base_ratio=0.3, monthly_rate=0.02)]
        out = rl.evaluate(px, 100_000, base, cands, per, ("A", "B"))
        self.assertEqual(out[0]["label"], "현재 규칙 (기준)")
        self.assertIsNone(out[0]["passed"])
        self.assertIn(out[1]["passed"], (True, False))
        self.assertEqual(set(out[1]["judge"]), {"A", "B"})


# ─────────────────────────────────────────
# shadow_trader
# ─────────────────────────────────────────

class ShadowSanitizeTest(unittest.TestCase):
    STATE = {"cash": 1000.0, "qty": 5, "avg": 70.0}

    def test_buy_is_capped_by_cash(self):
        d = st.sanitize({"action": "buy", "quantity": 999, "confidence": 0.9, "regime": "bull", "rationale": "x"},
                        self.STATE, 100.0)
        self.assertEqual(d["action"], "buy")
        self.assertEqual(d["quantity"], st.max_affordable(1000.0, 100.0))   # 9주 (슬리피지 포함)

    def test_sell_is_capped_by_holdings(self):
        d = st.sanitize({"action": "sell", "quantity": 50, "confidence": 1.5, "regime": "zzz", "rationale": "y" * 500},
                        self.STATE, 100.0)
        self.assertEqual((d["action"], d["quantity"]), ("sell", 5))
        self.assertEqual(d["confidence"], 1.0)
        self.assertEqual(d["regime"], "range")
        self.assertEqual(len(d["rationale"]), 300)

    def test_garbage_becomes_hold(self):
        self.assertEqual(st.sanitize({"action": "buy", "quantity": "many"}, self.STATE, 100.0)["action"], "hold")
        self.assertEqual(st.sanitize(None, self.STATE, 100.0)["action"], "hold")
        self.assertEqual(st.sanitize({"action": "sell", "quantity": 1}, {"cash": 0, "qty": 0, "avg": 0}, 100.0)["action"], "hold")


class ShadowApplyTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, val in (("SHADOW_DIR", self.tmp.name),
                          ("DECISIONS", os.path.join(self.tmp.name, "decisions.csv")),
                          ("STATE_FILE", os.path.join(self.tmp.name, "state.json"))):
            p = mock.patch.object(st, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.state = {"start_date": "2026-10-23", "start_total": 10_000.0, "bot_start_total": 10_000.0,
                      "cash": 10_000.0, "qty": 0, "avg": 0.0, "peak_total": 10_000.0,
                      "max_dd_pct": 0.0, "days": 0, "last_date": None}

    def test_buy_then_sell_math_and_drawdown(self):
        s = st.apply(self.state, {"action": "buy", "quantity": 10, "confidence": 0.5, "regime": "bull", "rationale": "a"},
                     100.0, date(2026, 10, 23), bot_total=10_000.0)
        self.assertAlmostEqual(s["cash"], 10_000 - 10 * 100 * 1.003, places=2)
        self.assertEqual(s["qty"], 10)
        self.assertAlmostEqual(s["avg"], 100.3, places=4)
        s = st.apply(s, {"action": "hold", "quantity": 0, "confidence": 0.5, "regime": "range", "rationale": "b"},
                     80.0, date(2026, 10, 24), bot_total=9_900.0)
        self.assertLess(s["max_dd_pct"], 0)                         # 80 으로 떨어져 낙폭 기록
        s = st.apply(s, {"action": "sell", "quantity": 10, "confidence": 0.5, "regime": "bear", "rationale": "c"},
                     90.0, date(2026, 10, 27))
        self.assertEqual(s["qty"], 0)
        self.assertEqual(s["avg"], 0.0)
        self.assertEqual(s["days"], 3)
        rows = list(csv.DictReader(open(st.DECISIONS, newline="", encoding="utf-8-sig")))
        self.assertEqual([r["행동"] for r in rows], ["buy", "hold", "sell"])
        self.assertEqual(rows[0]["봇총자산"], "10000.00")

    def test_report_text_compares_and_grades(self):
        s = st.apply(self.state, {"action": "buy", "quantity": 10, "confidence": 0.5, "regime": "bull", "rationale": "a"},
                     100.0, date(2026, 10, 23), bot_total=10_000.0)
        st.save_state(s)
        with mock.patch.object(st, "load_state", return_value=s):
            text = st.report_text(for_audit=True)
        self.assertIn("그림자 트레이더 vs 규칙 봇", text)
        self.assertIn("졸업 기준 미달", text)             # 1거래일뿐
        self.assertIn("거래일 1 ≥ 60", text)

    def test_report_without_state(self):
        with mock.patch.object(st, "load_state", return_value={}):
            self.assertIn("아직", st.report_text())


class ShadowInputsTest(unittest.TestCase):

    def test_price_is_read_from_bot_log(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(st, "LOG_DIR", tmp.name):
            self.assertEqual(st.todays_bot_price(date(2026, 10, 21)), (None, ""))
            with open(os.path.join(tmp.name, "trade_20261021.log"), "w", encoding="utf-8") as f:
                f.write("2026-10-21 22:29:20,000 [INFO] [1] strategy - TQQQ 현재가: $71.72\n")
                f.write("2026-10-21 22:29:30,000 [INFO] [1] strategy - GBV매도: TQQQ 4주 @ 71.72\n")
            self.assertEqual(st.todays_bot_price(date(2026, 10, 21)), (71.72, "bot-log"))

    def test_snapshot_from_report(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "미국장.csv"), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["기록시각", "현금잔고", "총자산", "매매구분", "잔고종목", "보유수량", "평균단가", "현재가"])
            w.writerow(["2026-10-21 22:29:42", "88,466.71", "98,937.82", "매도", "", "", "", ""])
            w.writerow(["", "", "", "", "TQQQ", "146", "77.79", "71.72"])
            w.writerow(["──────────", "", "", "", "", "", "", ""])
        with mock.patch.object(st, "REPORT_DIR", tmp.name):
            snap = st.bot_snapshot()
        self.assertEqual(snap["qty"], 146)
        self.assertAlmostEqual(snap["cash"], 88_466.71)
        self.assertEqual(snap["date"], "2026-10-21")
        state = st.init_state_from_bot(snap, date(2026, 10, 23))
        self.assertAlmostEqual(state["start_total"], 88_466.71 + 146 * 71.72, places=2)
        self.assertEqual(state["qty"], 146)

    def test_derived_stats_and_message(self):
        px = {"TQQQ": series([70 + i * 0.1 for i in range(80)]), "QQQ": series([500] * 80)}
        stats = st.derived_stats(px["TQQQ"])
        for k in ("last", "ret_5d_pct", "ret_20d_pct", "drawdown_from_high_pct", "vol_20d_annual_pct", "ma20", "ma60"):
            self.assertIn(k, stats)
        state = {"cash": 1000.0, "qty": 2, "avg": 70.0, "start_total": 1140.0, "start_date": "2026-10-01",
                 "days": 3, "max_dd_pct": -1.0}
        msg = st.build_user_message(state, 77.9, "bot-log", px, date(2026, 10, 23))
        self.assertIn("$77.90", msg)
        self.assertIn("매수 가능 최대", msg)
        self.assertNotIn("sk-ant", msg)
        self.assertIn('"last": 77.9', msg)                      # 오늘 시세가 지표의 마지막 점

    def test_seconds_until_bot_cycle_follows_resolved_market_time(self):
        from datetime import datetime
        with (mock.patch("config_manager.load_config", return_value={}),
              mock.patch("config_manager.get_market_times", return_value=("23:30", ""))):
            d = date(2026, 11, 3)                                   # 서머타임 해제 뒤: 23:30 KST
            self.assertEqual(st.seconds_until_bot_cycle(d, datetime(2026, 11, 3, 22, 35)), 60 * 60)
            self.assertEqual(st.seconds_until_bot_cycle(d, datetime(2026, 11, 3, 23, 40)), 0)
            self.assertEqual(st.seconds_until_bot_cycle(d, datetime(2026, 11, 3, 10, 0)), 90 * 60)   # 상한
            self.assertEqual(st.seconds_until_bot_cycle(date(2026, 11, 2), datetime(2026, 11, 3, 22, 35)), 0)  # 과거 날짜
        with (mock.patch("config_manager.load_config", return_value={}),
              mock.patch("config_manager.get_market_times", return_value=("", "09:05"))):
            self.assertEqual(st.seconds_until_bot_cycle(d, datetime(2026, 11, 3, 22, 35)), 0)   # 미국장 꺼짐
        with mock.patch("config_manager.load_config", side_effect=OSError("no config")):
            self.assertEqual(st.seconds_until_bot_cycle(d, datetime(2026, 11, 3, 22, 35)), 0)

    def test_wait_for_bot_price_gives_up_without_blocking(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(st, "LOG_DIR", tmp.name):
            self.assertEqual(st.wait_for_bot_price(date(2026, 10, 21), timeout_sec=0), (None, ""))

    def test_decisions_csv_records_price_source(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with (mock.patch.object(st, "SHADOW_DIR", tmp.name),
              mock.patch.object(st, "DECISIONS", os.path.join(tmp.name, "d.csv"))):
            state = {"cash": 100.0, "qty": 0, "avg": 0.0, "peak_total": 100.0, "max_dd_pct": 0.0, "days": 0}
            st.apply(state, {"action": "hold", "quantity": 0, "confidence": 0.1, "regime": "range", "rationale": ""},
                     50.0, date(2026, 10, 23), 100.0, source="yfinance-1m")
            rows = list(csv.DictReader(open(st.DECISIONS, newline="", encoding="utf-8-sig")))
        self.assertEqual(rows[0]["출처"], "yfinance-1m")


class ShadowDecideTest(unittest.TestCase):

    def _client(self, text, stop="end_turn"):
        msg = mock.Mock(stop_reason=stop, model="claude-opus-5",
                        content=[mock.Mock(type="text", text=text)],
                        usage=mock.Mock(input_tokens=1, output_tokens=1, cache_read_input_tokens=0))
        stream = mock.MagicMock()
        stream.__enter__.return_value.get_final_message.return_value = msg
        client = mock.Mock()
        client.beta.messages.stream.return_value = stream
        return client

    def test_structured_json_is_parsed_and_request_is_shaped(self):
        c = self._client(json.dumps({"action": "buy", "quantity": 3, "confidence": 0.6, "regime": "bull", "rationale": "r"}))
        d = st.decide(c, "자료")
        self.assertEqual(d["quantity"], 3)
        kw = c.beta.messages.stream.call_args.kwargs
        self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(kw["fallbacks"], "default")
        self.assertNotIn("thinking", kw)

    def test_refusal_becomes_hold(self):
        self.assertEqual(st.decide(self._client("", stop="refusal"), "자료")["action"], "hold")

    def test_schema_rejection_falls_back_to_plain_json(self):
        good = self._client(json.dumps({"action": "hold", "quantity": 0, "confidence": 0.2, "regime": "range", "rationale": "r"}))
        stream_ok = good.beta.messages.stream.return_value
        c = mock.Mock()
        c.beta.messages.stream.side_effect = [
            Exception("Error code: 400 - output_config.format.schema: For 'array' type, property 'maxItems' is not supported"),
            stream_ok]
        d = st.decide(c, "자료")
        self.assertEqual(d["action"], "hold")
        self.assertEqual(c.beta.messages.stream.call_count, 2)
        retry_kwargs = c.beta.messages.stream.call_args_list[1].kwargs
        self.assertNotIn("output_config", retry_kwargs)
        self.assertIn("JSON 객체 하나만", retry_kwargs["messages"][0]["content"])

    def test_other_api_errors_are_not_swallowed(self):
        c = mock.Mock()
        c.beta.messages.stream.side_effect = Exception("Error code: 529 - overloaded")
        with self.assertRaises(Exception):
            st.decide(c, "자료")
        self.assertEqual(c.beta.messages.stream.call_count, 1)

    def test_schemas_use_only_supported_keywords(self):
        banned = {"minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength", "pattern"}

        def walk(node):
            if isinstance(node, dict):
                self.assertFalse(banned & set(node), f"지원 안 되는 키워드: {banned & set(node)}")
                if "type" in node:
                    self.assertIsInstance(node["type"], str, "type 배열 대신 anyOf 를 쓴다")
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(st.DECISION_SCHEMA)
        walk(ar.PROPOSAL_SCHEMA)

    def test_prose_wrapped_json_still_parses(self):
        d = st.decide(self._client('판단: {"action": "sell", "quantity": 1, "confidence": 0.4, "regime": "bear", "rationale": "x"} 끝'), "자료")
        self.assertEqual(d["action"], "sell")


# ─────────────────────────────────────────
# ai_researcher
# ─────────────────────────────────────────

class ResearcherTest(unittest.TestCase):

    def test_parse_proposals_filters_bad_ones(self):
        raw = [
            {"name": "A", "hypothesis": "h", "params": {"base_ratio": 0.3, "monthly_rate": 0.02, "pause_below_cash": None,
                                                        "bump_drawdown": None, "bump_pct": None, "cap_ratio": None}},
            {"name": "B", "hypothesis": "h", "params": {"base_ratio": 0.99, "monthly_rate": 0.02, "pause_below_cash": None,
                                                        "bump_drawdown": None, "bump_pct": None, "cap_ratio": None}},
            {"name": "C", "hypothesis": "h", "params": {"base_ratio": 0.3, "monthly_rate": 0.02, "pause_below_cash": None,
                                                        "bump_drawdown": 0.5, "bump_pct": None, "cap_ratio": None}},
        ]
        good, bad = ar.parse_proposals(raw)
        self.assertEqual([n for n, _, _ in good], ["A"])
        self.assertEqual([n for n, _ in bad], ["B", "C"])

    def test_memo_and_summary(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        px = crash_and_rebound(400)
        per = {"코로나 2020": ("2022-01-01", "2022-06-30"), "2022 하락": ("2022-07-01", "2022-12-31"),
               "2022-23 회복": ("2022-07-01", "2023-03-31"), "최근 12개월": ("2023-01-01", "2023-07-31")}
        self.assertEqual(set(ar.CRISIS), set(per) - {"최근 12개월"})
        base = rl.RuleParams(base_ratio=0.1, monthly_rate=0.02)
        cand = rl.RuleParams(base_ratio=0.3, monthly_rate=0.02)
        ev = rl.evaluate(px, 100_000, base, [cand], per, ar.CRISIS)
        names = {id(cand): ("세 배 기준금", "낙폭 구간에서 더 산다")}
        with mock.patch.object(ar, "RESEARCH_DIR", tmp.name):
            path = ar.write_memo(date(2026, 10, 26), base, 100_000, ev, names, [("X", "범위 밖")], list(per))
        text = open(path, encoding="utf-8").read()
        self.assertIn("세 배 기준금", text)
        self.assertIn("X: 범위 밖", text)
        self.assertIn("설정은 바꾸지 않았다" if ev[1]["passed"] else "그대로 가장 낫다", text)
        s = ar.summary_text(date(2026, 10, 26), ev, names, [("X", "범위 밖")])
        self.assertTrue(s.startswith("🔬"))
        self.assertIn("설정은 바꾸지 않았습니다", s)
        self.assertLessEqual(len(s), ar.TELEGRAM_LIMIT)

    def test_schema_requires_every_param_key(self):
        req = ar.PROPOSAL_SCHEMA["properties"]["proposals"]["items"]["properties"]["params"]["required"]
        self.assertEqual(set(req), set(rl.BOUNDS))
