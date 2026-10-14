"""
risk_table.py
기준금 비중별 백테스트 성적표

/set 으로 기준금을 크게 바꿀 때, 그 비중이 2022 하락장에서 어떻게 됐는지 확인 메시지에
같이 보여 준다. 손실 회복이 급할 때 레버리지를 올리는 충동을 숫자로 한 번 막는 용도다.
LLM 은 필요 없다. 표를 찾아 쓰는 것뿐이다.

수치 출처: backtest_dual.py, 2022-01-01 ~ 2023-12-31, 월 증액 2%, 유휴 현금 금리 0%.
  - TQQQ: 달러 자본 대비 기준금 비중
  - 국내 S&P500 1배 ETF(360750 등): 원화 자본 대비 기준금 비중 (SPY×환율 합성, 1일 시차)
합성·단일 구간이므로 '그 해에 그랬다'이지 보장이 아니다. 메시지에도 그렇게 적는다.
"""

import csv
import os

REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")

# 비중 → (수익률 %, MDD %, 최저현금 % of 자본)
TQQQ_2022 = {
    0.10: (+4.5,  -13.7, 73.6),
    0.20: (+8.9,  -27.4, 47.1),
    0.30: (+13.4, -41.0, 20.7),
    0.35: (+15.7, -47.9,  7.5),
    0.40: (+14.9, -56.5,  0.1),
    0.45: (+7.5,  -64.0,  0.1),
    0.50: (-7.3,  -68.8,  0.0),
    0.60: (-24.4, -75.6,  0.7),
    0.70: (-33.1, -76.9,  0.1),
}
KR_SP500_2022 = {
    0.30: (+7.4,  -5.9,  55.6),
    0.50: (+12.5, -9.8,  26.0),
    0.60: (+15.1, -11.7, 11.3),
    0.70: (+17.4, -13.7,  0.1),
    0.80: (+11.0, -15.1,  0.1),
}
# 현금 고갈이 시작되는 비중 (그 이상은 하락장에서 매수가 막혔다)
CLIFF = {"TQQQ": 0.40, "KR": 0.70}

# 표가 적용되는 종목만. 레버리지 국내 ETF(418660 등)에 1배 표를 보여 주면 오히려 오도한다.
KR_SP500_1X = {"360750", "379800", "379780", "449180"}


def _table_for(ticker: str):
    t = ticker.upper().lstrip("A")
    if t == "TQQQ":
        return TQQQ_2022, "TQQQ", "$"
    if t in KR_SP500_1X:
        return KR_SP500_2022, "KR", "₩"
    return None, None, None


def nearest(table: dict, ratio: float):
    """표에서 비중이 가장 가까운 행 (키, 값). 표 범위를 벗어나도 끝 행을 돌려준다"""
    key = min(table, key=lambda k: abs(k - ratio))
    return key, table[key]


def latest_total_assets(market: str) -> float:
    """reports/<market>.csv 의 마지막 총자산. 없으면 0"""
    path = os.path.join(REPORT_DIR, f"{market}.csv")
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    except OSError:
        return 0.0
    if not rows:
        return 0.0
    try:
        col = rows[0].index("총자산")
    except ValueError:
        return 0.0
    for r in reversed(rows[1:]):
        if len(r) > col and r[col].strip():
            try:
                return float(r[col].replace(",", ""))
            except ValueError:
                continue
    return 0.0


def risk_note(ticker: str, base: float, capital: float = None) -> str:
    """확인 메시지에 붙일 한 줄. 표가 없는 종목이거나 자본을 모르면 빈 문자열"""
    table, kind, unit = _table_for(ticker)
    if table is None or base <= 0:
        return ""
    if capital is None:
        capital = latest_total_assets("미국장" if kind == "TQQQ" else "국내장")
    if not capital or capital <= 0:
        return ""
    ratio = base / capital
    key, (ret, mdd, cash) = nearest(table, ratio)
    warn = "  ⚠ 현금 고갈 구간" if ratio >= CLIFF[kind] else ""
    return (f"📊 비중 {ratio:.0%} (자본 {unit}{capital:,.0f}) → 2022 하락장 백테스트 "
            f"{key:.0%} 행: 수익 {ret:+.1f}% / MDD {mdd:.1f}% / 최저현금 {cash:.1f}%{warn}\n"
            f"   (한 구간의 결과이지 보장이 아닙니다)")
