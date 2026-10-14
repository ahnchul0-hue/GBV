"""
backtest_dual.py 단위 테스트 (네트워크 없이 합성 가격으로)

실행: python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("GBV_LOG_FILE", os.devnull)

import pandas as pd  # noqa: E402
import backtest_dual as bd  # noqa: E402


def series(values, start="2022-01-03"):
    """영업일 간격 가격 시계열"""
    idx = pd.bdate_range(start=start, periods=len(values))
    return pd.Series([float(v) for v in values], index=idx)


class RunBucketTest(unittest.TestCase):

    def test_first_buy_fills_the_base(self):
        """기준금 1,000 / 가격 100 → 수수료 감안 9주 (10주면 초과)"""
        px = series([100] * 5)
        df = bd.run_bucket(px, capital=10_000, base=1_000, monthly_rate=0,
                           start="2022-01-01", end="2022-12-31")
        self.assertEqual(df["qty"].iloc[0], 9)
        self.assertIn("매수 9", df["action"].iloc[0])

    def test_commission_in_sizing_buys_one_less(self):
        """기존 backtest_interactive 는 수수료를 빼지 않아 한 주 더 산다"""
        px = series([100] * 3)
        kw = dict(capital=10_000, base=1_000, monthly_rate=0,
                  start="2022-01-01", end="2022-12-31")
        bot = bd.run_bucket(px, commission_in_sizing=True, **kw)
        old = bd.run_bucket(px, commission_in_sizing=False, **kw)
        self.assertEqual(bot["qty"].iloc[0], 9)
        self.assertEqual(old["qty"].iloc[0], 10)

    def test_price_fall_triggers_buy_and_rise_triggers_sell(self):
        px = series([100, 50, 200])
        df = bd.run_bucket(px, capital=10_000, base=1_000, monthly_rate=0,
                           start="2022-01-01", end="2022-12-31")
        self.assertIn("매수", df["action"].iloc[1])      # 떨어지면 더 산다
        self.assertIn("매도", df["action"].iloc[2])      # 오르면 판다

    def test_insufficient_cash_skips_the_whole_order(self):
        """봇은 수량을 차액으로 정하므로 잔액이 모자라면 전량 반려된다"""
        px = series([100, 100])
        df = bd.run_bucket(px, capital=500, base=1_000, monthly_rate=0,
                           start="2022-01-01", end="2022-12-31")
        self.assertEqual(df["qty"].iloc[0], 0, "부분 체결되면 안 된다")
        self.assertEqual(df["action"].iloc[0], "")
        self.assertGreater(df["shortfall"].iloc[0], 0)
        self.assertEqual(df["cash"].iloc[0], 500)        # 현금은 그대로

    def test_monthly_increase_applies_on_the_first_day_then_monthly(self):
        """첫 실행도 그 달 첫 매매라 증액된다 (2026-09-30 실측과 같은 동작)"""
        px = series([100] * 45, start="2022-01-03")      # 1월 → 2월 → 3월
        df = bd.run_bucket(px, capital=100_000, base=1_000, monthly_rate=0.10,
                           start="2022-01-01", end="2022-12-31")
        months = df.index.to_series().dt.strftime("%Y-%m")
        firsts = df.groupby(months)["base"].first()
        self.assertAlmostEqual(firsts.iloc[0], 1_100.0, places=6)   # 1,000 × 1.1
        self.assertAlmostEqual(firsts.iloc[1], 1_210.0, places=6)
        self.assertAlmostEqual(firsts.iloc[2], 1_331.0, places=6)

    def test_cash_interest_accrues_on_idle_cash(self):
        px = series([100] * 60)
        kw = dict(capital=100_000, base=1_000, monthly_rate=0,
                  start="2022-01-01", end="2022-12-31")
        flat = bd.run_bucket(px, annual_cash_rate=0.0, **kw)
        paid = bd.run_bucket(px, annual_cash_rate=0.05, **kw)
        self.assertGreater(paid["total"].iloc[-1], flat["total"].iloc[-1])
        days = (paid.index[-1] - paid.index[0]).days
        gain = paid["total"].iloc[-1] / flat["total"].iloc[-1] - 1
        self.assertLess(gain, 0.05 * days / 365 * 1.1)   # 대략 기간 비례

    def test_short_series_is_rejected(self):
        with self.assertRaises(ValueError):
            bd.run_bucket(series([100]), 10_000, 1_000, 0, "2022-01-01", "2022-12-31")


class SyntheticTest(unittest.TestCase):

    def test_krw_series_is_price_times_fx_shifted_one_day(self):
        """국내 ETF 는 한국 장중에 전날 미국 종가를 반영한다"""
        usd = series([100, 110, 120])
        fx = series([1_000, 1_000, 1_000])
        s = bd.synthetic_krw(usd, fx, shift_days=1)
        self.assertEqual(len(s), 2)
        self.assertAlmostEqual(s.iloc[0], 100_000)   # 첫날 종가가 둘째날 가격
        self.assertAlmostEqual(s.iloc[1], 110_000)

    def test_no_shift_option(self):
        usd = series([100, 110])
        fx = series([1_000, 1_000])
        s = bd.synthetic_krw(usd, fx, shift_days=0)
        self.assertAlmostEqual(s.iloc[0], 100_000)


class CombineTest(unittest.TestCase):

    def test_sums_both_buckets_in_krw(self):
        idx = pd.bdate_range("2022-01-03", periods=3)
        us = pd.DataFrame({"total": [100.0, 200.0, 300.0]}, index=idx)
        kr = pd.DataFrame({"total": [1_000.0, 1_000.0, 1_000.0]}, index=idx)
        fx = pd.Series([1_300.0] * 3, index=idx)
        out = bd.combine(us, kr, fx)
        self.assertAlmostEqual(out["total_krw"].iloc[0], 100 * 1_300 + 1_000)
        self.assertAlmostEqual(out["total_krw"].iloc[-1], 300 * 1_300 + 1_000)

    def test_only_shared_dates_are_kept(self):
        us = pd.DataFrame({"total": [1.0, 2.0, 3.0]}, index=pd.bdate_range("2022-01-03", periods=3))
        kr = pd.DataFrame({"total": [10.0, 20.0]}, index=pd.bdate_range("2022-01-04", periods=2))
        fx = pd.Series([1_300.0] * 5, index=pd.bdate_range("2022-01-03", periods=5))
        self.assertEqual(len(bd.combine(us, kr, fx)), 2)


class SummarizeTest(unittest.TestCase):

    def test_reports_drawdown_and_cash_floor(self):
        px = series([100, 50, 100])
        df = bd.run_bucket(px, capital=10_000, base=5_000, monthly_rate=0,
                           start="2022-01-01", end="2022-12-31")
        m = bd.summarize(df, 10_000)
        self.assertLess(m["MDD%"], 0)
        self.assertLessEqual(m["최저현금"], 10_000)
        self.assertGreaterEqual(m["목표미달평균%"], 0)
