"""
rules_lab.py
규칙 변형을 돌려 보는 백테스트 엔진 (AI 리서처용)

backtest_dual.run_bucket 과 같은 루프에 '훅' 세 개를 더한 것이다. 훅을 모두 끄면
run_bucket 과 센트 단위로 같다 — 테스트가 이 동일성을 고정한다. AI 리서처가 제안할
수 있는 것은 이 훅의 숫자뿐이다. 코드가 아니라 파라미터만 바꾸므로 제안은 전부
여기서 같은 조건으로 검증된다.

훅 (RuleParams)
  base_ratio        기준금 = 자본 × 비율                     (기본 전략의 '기준금')
  monthly_rate      월 증액률                                (기본 전략의 '월 증액')
  pause_below_cash  현금이 기준금의 이 배수 미만이면 그달 증액을 건너뜀 (None = 끔)
                    2026-10-20 수기 실험에서 모든 구간 수익이 줄었던 규칙. 반례로 남겨 둔다.
  bump_drawdown     고점 대비 낙폭이 이 비율 이상이면 기준금을 bump_pct 만큼 한 번 올림 (None = 끔)
                    같은 낙폭 구간에서 한 번만. 가격이 고점을 갱신하면 다시 허용.
  bump_pct          위 증가율
  cap_ratio         기준금이 (현금 + 평가액) × 비율을 넘지 못하게 함 (None = 끔)

run_bucket 과의 차이는 훅뿐이다. 수량 산정에 수수료를 반영하는 것(strategy.py:69)도 같다.
"""

from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd

COMMISSION = 0.0015

# 제안 가능한 범위. 이 밖은 ValueError — AI 가 극단값으로 '이기는' 것을 막는다.
BOUNDS = {
    "base_ratio":       (0.05, 0.90),
    "monthly_rate":     (0.00, 0.05),
    "pause_below_cash": (0.00, 5.00),
    "bump_drawdown":    (0.10, 0.90),
    "bump_pct":         (0.00, 1.00),
    "cap_ratio":        (0.10, 1.00),
}


@dataclass
class RuleParams:
    base_ratio: float = 0.10
    monthly_rate: float = 0.02
    pause_below_cash: Optional[float] = None
    bump_drawdown: Optional[float] = None
    bump_pct: Optional[float] = None
    cap_ratio: Optional[float] = None

    def validate(self) -> "RuleParams":
        for name, (lo, hi) in BOUNDS.items():
            v = getattr(self, name)
            if v is None:
                if name in ("base_ratio", "monthly_rate"):
                    raise ValueError(f"{name} 은 필수")
                continue
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise ValueError(f"{name} 은 숫자여야 함: {v!r}")
            if not (lo <= float(v) <= hi):
                raise ValueError(f"{name}={v} 는 허용 범위 {lo}~{hi} 밖")
        if (self.bump_drawdown is None) != (self.bump_pct is None):
            raise ValueError("bump_drawdown 과 bump_pct 는 함께 주거나 함께 비워야 함")
        return self

    def hooks_off(self) -> bool:
        return self.pause_below_cash is None and self.bump_drawdown is None and self.cap_ratio is None

    def label(self) -> str:
        parts = [f"기준금 {self.base_ratio:.0%}", f"월 {self.monthly_rate:.1%}"]
        if self.pause_below_cash is not None:
            parts.append(f"현금<{self.pause_below_cash:g}×기준금이면 증액중지")
        if self.bump_drawdown is not None:
            parts.append(f"낙폭 {self.bump_drawdown:.0%} 시 기준금 +{self.bump_pct:.0%}")
        if self.cap_ratio is not None:
            parts.append(f"기준금 상한 자산의 {self.cap_ratio:.0%}")
        return " / ".join(parts)

    @classmethod
    def from_dict(cls, d: dict) -> "RuleParams":
        allowed = {k: d.get(k) for k in BOUNDS}
        return cls(**allowed).validate()

    def to_dict(self) -> dict:
        return asdict(self)


def run_rules(prices: pd.Series, capital: float, p: RuleParams, start: str, end: str,
              commission: float = COMMISSION, annual_cash_rate: float = 0.0) -> pd.DataFrame:
    """한 통화 버킷의 GBV 리밸런싱 + 훅. 열 구성은 backtest_dual.run_bucket 과 같다"""
    p.validate()
    px = prices.loc[start:end].dropna()
    if len(px) < 2:
        raise ValueError(f"가격 데이터가 부족합니다: {len(px)}일")

    cash, qty = capital, 0
    cur_base = capital * p.base_ratio
    month, prev_day = None, None
    peak, bumped = None, False
    rows = []

    for day, price in px.items():
        price = float(price)
        if price <= 0:
            continue

        if prev_day is not None and annual_cash_rate:
            cash *= 1 + annual_cash_rate * (day - prev_day).days / 365.0
        prev_day = day

        # 월 증액 (달이 바뀐 첫 거래일, 첫날 포함 — run_bucket 과 동일). pause 훅은 여기만 건드린다.
        m = day.strftime("%Y-%m")
        if m != month:
            if p.pause_below_cash is None or cash >= p.pause_below_cash * cur_base:
                cur_base *= 1 + p.monthly_rate
            month = m

        # 낙폭 훅: 고점 갱신 시 재무장, 낙폭 기준 도달 시 한 번만
        if peak is None or price > peak:
            peak, bumped = price, False
        if p.bump_drawdown is not None and not bumped and price <= peak * (1 - p.bump_drawdown):
            cur_base *= 1 + p.bump_pct
            bumped = True

        # 상한 훅
        if p.cap_ratio is not None:
            cur_base = min(cur_base, p.cap_ratio * (cash + qty * price))

        diff = cur_base - qty * price
        action, short = "", 0.0
        if abs(diff) >= price:
            if diff > 0:
                want = int(diff / (price * (1 + commission)))
                cost = want * price * (1 + commission)
                if cost <= cash:
                    qty += want
                    cash -= cost
                    action = f"매수 {want}"
                else:
                    short = cost          # 전량 거부 (봇과 같다)
            else:
                want = min(int(-diff / price), qty)
                if want > 0:
                    qty -= want
                    cash += want * price * (1 - commission)
                    action = f"매도 {want}"

        rows.append({"date": day, "price": price, "cash": cash, "qty": qty,
                     "position": qty * price, "total": cash + qty * price,
                     "base": cur_base, "action": action, "shortfall": short})

    return pd.DataFrame(rows).set_index("date")


def summarize(df: pd.DataFrame, capital: float) -> dict:
    tot = df["total"]
    gap = (df["base"] - df["position"]).clip(lower=0) / df["base"]
    return {
        "최종": float(tot.iloc[-1]),
        "수익률%": float((tot.iloc[-1] / capital - 1) * 100),
        "MDD%": float(((tot / tot.cummax()) - 1).min() * 100),
        "최저현금%": float(df["cash"].min() / capital * 100),
        "목표미달평균%": float(gap.mean() * 100),
        "자금부족일": int((df["shortfall"] > 0).sum()),
    }


# ─────────────────────────────────────────
# 제안 채점
# ─────────────────────────────────────────

# 통과 조건. 한 구간에 맞춘 미세 개선을 걸러내려 두 위기 구간 모두 이겨야 한다.
PASS_MIN_RETURN_GAIN_PT = 0.5     # 기준 대비 수익률 +0.5%p 이상
PASS_MAX_MDD_WORSE_PT = 5.0       # MDD 가 기준보다 5%p 넘게 나빠지면 탈락


def judge(candidate: dict, baseline: dict) -> tuple:
    """(통과 여부, 사유). candidate/baseline 은 summarize() 결과"""
    if candidate["자금부족일"] > 0:
        return False, f"자금부족 {candidate['자금부족일']}일"
    gain = candidate["수익률%"] - baseline["수익률%"]
    if gain < PASS_MIN_RETURN_GAIN_PT:
        return False, f"수익률 {gain:+.1f}%p (기준 +{PASS_MIN_RETURN_GAIN_PT}%p 미만)"
    worse = baseline["MDD%"] - candidate["MDD%"]      # 양수면 후보가 더 깊이 빠짐
    if worse > PASS_MAX_MDD_WORSE_PT:
        return False, f"MDD {worse:.1f}%p 악화 (허용 {PASS_MAX_MDD_WORSE_PT}%p)"
    return True, f"수익률 {gain:+.1f}%p, MDD {-worse:+.1f}%p"


def evaluate(prices: pd.Series, capital: float, baseline: RuleParams,
             candidates: list, periods: dict, crisis_keys: tuple) -> list:
    """모든 후보를 모든 구간에 돌린다.

    periods: {이름: (start, end)}, crisis_keys: 통과 판정에 쓰는 구간 이름들.
    Returns: [{"params", "label", "results": {구간: summarize}, "judge": {구간: (통과, 사유)},
               "passed": bool}] — baseline 이 첫 원소
    """
    out = []
    base_res = {k: summarize(run_rules(prices, capital, baseline, a, b), capital)
                for k, (a, b) in periods.items()}
    out.append({"params": baseline, "label": "현재 규칙 (기준)", "results": base_res,
                "judge": {}, "passed": None})
    for cand in candidates:
        res, verdict = {}, {}
        for k, (a, b) in periods.items():
            try:
                res[k] = summarize(run_rules(prices, capital, cand, a, b), capital)
            except ValueError as e:
                res[k] = None
                verdict[k] = (False, f"실행 불가: {e}")
                continue
            if k in crisis_keys:
                verdict[k] = judge(res[k], base_res[k])
        passed = all(verdict.get(k, (False, ""))[0] for k in crisis_keys)
        out.append({"params": cand, "label": cand.label(), "results": res,
                    "judge": verdict, "passed": passed})
    return out
