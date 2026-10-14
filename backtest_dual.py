"""
backtest_dual.py
달러·원화 두 버킷을 동시에 돌리는 GBV 백테스트

backtest_interactive.py 와 다른 점
  1) 두 통화를 분리한다. 봇은 달러 버킷(get_us_balance)과 원화 버킷
     (get_kr_balance)을 완전히 나눠 쓰므로, 한쪽 현금이 말라도 다른 쪽은
     영향을 받지 않는다. 한 통화로 뭉쳐 돌리면 이 격리가 사라져 결과가
     낙관적으로 나온다.
  2) 유휴 현금에 금리를 준다. 기준금 비중이 낮으면 자본 대부분이 현금인데
     금리를 0 으로 두면 낮은 비중이 실제보다 불리하게 평가된다.
  3) 매수 자금이 모자라면 건너뛴 사실을 기록한다. 조용히 넘기면 전략이
     현금 제약을 받았는지 알 수 없다.

국내 상장 해외 ETF 를 합성으로 만들 때는 shift_days=1 을 써야 한다.
국내 ETF 는 한국 장중에 '전날' 미국 종가를 반영하므로, 날짜를 밀지 않으면
하루 앞선 가격으로 매매하는 셈이 된다(실측 상관 0.04 → 0.64).
"""

import pandas as pd

TRADING_DAYS = 252


def synthetic_krw(usd_close: pd.Series, usdkrw: pd.Series,
                  shift_days: int = 1) -> pd.Series:
    """달러 자산 + 환율 → 원화 환노출 상품 (국내 상장 해외 ETF 근사)"""
    fx = usdkrw.reindex(usd_close.index).ffill()
    return (usd_close * fx).shift(shift_days).dropna()


def run_bucket(prices: pd.Series, capital: float, base: float,
               monthly_rate: float, start: str, end: str,
               commission: float = 0.0015,
               annual_cash_rate: float = 0.0,
               commission_in_sizing: bool = True) -> pd.DataFrame:
    """한 통화 버킷의 GBV 리밸런싱

    prices 는 그 통화로 표시된 종가. capital·base 도 같은 통화.

    commission_in_sizing: 봇(strategy.py:69)은 수량을 abs(diff)/(price*1.0015)
        로 정해 수수료만큼 덜 산다. backtest_interactive.py:302 는 수수료를
        빼지 않아 봇보다 많이 사므로, 그쪽과 비교할 때만 False 로 둔다.
    """
    px = prices.loc[start:end].dropna()
    if len(px) < 2:
        raise ValueError(f"가격 데이터가 부족합니다: {len(px)}일")

    cash, qty, cur_base, month = capital, 0, base, None
    prev_day, rows = None, []

    for day, price in px.items():
        price = float(price)
        if price <= 0:
            continue

        # 유휴 현금 이자 (달력 일수 기준)
        if prev_day is not None and annual_cash_rate:
            cash *= 1 + annual_cash_rate * (day - prev_day).days / 365.0
        prev_day = day

        # 월 증액: 달이 바뀐 첫 거래일
        # 봇도 첫 실행이 그 달의 첫 매매면 증액한다 (config 의 기록이 비어 있으므로).
        # 실제로 2026-09-30 첫 가동에서 $10,000 → $10,200 이 적용됐다.
        m = day.strftime("%Y-%m")
        if m != month:
            cur_base *= 1 + monthly_rate
            month = m

        diff = cur_base - qty * price
        action, short = "", 0.0
        if abs(diff) >= price:
            if diff > 0:
                divisor = price * (1 + commission) if commission_in_sizing else price
                want = int(diff / divisor)
                cost = want * price * (1 + commission)
                if cost <= cash:
                    qty += want
                    cash -= cost
                    action = f"매수 {want}"
                else:
                    # 전량 거부. 봇은 주문 수량을 차액으로 정하고 현금을 보지 않으므로
                    # 잔액이 모자라면 부분 체결이 아니라 주문 자체가 반려된다.
                    short = cost
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
    """버킷 성과 요약"""
    tot = df["total"]
    gap = (df["base"] - df["position"]).clip(lower=0) / df["base"]
    return {
        "최종": tot.iloc[-1],
        "수익률%": (tot.iloc[-1] / capital - 1) * 100,
        "MDD%": ((tot / tot.cummax()) - 1).min() * 100,
        "최저현금": df["cash"].min(),
        "최저현금%": df["cash"].min() / capital * 100,
        "목표미달평균%": gap.mean() * 100,
        "자금부족일": int((df["shortfall"] > 0).sum()),
    }


def combine(us: pd.DataFrame, kr: pd.DataFrame, usdkrw: pd.Series) -> pd.DataFrame:
    """두 버킷을 원화로 합산 (거래일이 다르므로 공통 날짜만)"""
    fx = usdkrw.reindex(us.index.union(kr.index)).ffill()
    idx = us.index.intersection(kr.index)
    return pd.DataFrame({
        "us_krw": us.loc[idx, "total"] * fx.loc[idx],
        "kr_krw": kr.loc[idx, "total"],
        "total_krw": us.loc[idx, "total"] * fx.loc[idx] + kr.loc[idx, "total"],
    })
