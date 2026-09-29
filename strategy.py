"""
strategy.py
GBV 전략 핵심 로직

주요 기능:
- GBV 리밸런싱 (월 증액 CDP)
- 월 자동 증액
- 달러/원화 완전 분리
"""

import logging
import time
from datetime import date

# 주문 후 잔고 반영 대기 시간 (초)
SETTLEMENT_WAIT_SEC = 3

from config_manager import (
    load_config, get_all_us_tickers, get_all_kr_tickers,
    get_base_value, get_monthly_rate,
    get_outside_tqqq, update_base, update_monthly_increase_date,
    get_last_increased_month, is_first_trading_day_of_month,
    normalize_kr_ticker
)
from notifier import (
    notify_buy, notify_sell, notify_error,
    notify_monthly_increase, notify_cycle_complete
)
from reporter import save_report

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════
# GBV 실행 (월 증액 CDP 리밸런싱)
# ════════════════════════════════════════════

def execute_gbv(api, ticker: str, price: float, base_value: float,
                outside_tqqq: int, is_us: bool, attempted: list = None) -> tuple:
    """
    GBV 리밸런싱
    attempted: 주문을 전송한 종목을 기록할 리스트 (성공 여부와 무관, 타임아웃 포함)
    Returns: (trades, new_holdings, new_cash)
    """
    trades = []
    
    # 현재 잔고 재조회
    if is_us:
        holdings, cash = api.get_us_balance()
        current_qty = holdings.get(ticker, {}).get("qty", 0)
    else:
        holdings, cash = api.get_kr_balance()
        # config 종목코드가 'A252670'처럼 접두어가 붙어 있어도 잔고(6자리)와 매칭
        info = holdings.get(ticker) or holdings.get(normalize_kr_ticker(ticker), {})
        current_qty = info.get("qty", 0)
    
    # TQQQ의 경우 outside 포함
    total_qty = current_qty
    if ticker == "TQQQ" and is_us:
        total_qty += outside_tqqq
    
    current_value = total_qty * price
    diff = base_value - current_value
    
    if abs(diff) >= price:
        if diff > 0:
            # 매수 (수수료 0.15% 감안)
            buy_qty = int(abs(diff) / (price * 1.0015))
            if buy_qty > 0:
                logger.info(f"GBV매수: {ticker} {buy_qty}주 @ ${price:.2f}")
                if attempted is not None:
                    attempted.append(ticker)
                if is_us:
                    success = api.buy_us(ticker, buy_qty)
                else:
                    success = api.buy_kr(ticker, buy_qty, int(price))
                
                if success:
                    trades.append({
                        "action": "매수", "ticker": ticker,
                        "qty": buy_qty, "price": price
                    })
                    notify_buy(ticker, buy_qty, price)
        else:
            # 매도 (OUTSIDE 제외)
            sell_qty = min(int(abs(diff) / price), current_qty)
            if sell_qty > 0:
                logger.info(f"GBV매도: {ticker} {sell_qty}주 @ ${price:.2f}")
                if attempted is not None:
                    attempted.append(ticker)
                if is_us:
                    success = api.sell_us(ticker, sell_qty)
                else:
                    success = api.sell_kr(ticker, sell_qty, int(price))
                
                if success:
                    trades.append({
                        "action": "매도", "ticker": ticker,
                        "qty": sell_qty, "price": price
                    })
                    notify_sell(ticker, sell_qty, price)
    
    # 매매 후 최신 잔고 반환 (체결 반영 대기)
    if not trades:
        return trades, holdings, cash
    time.sleep(SETTLEMENT_WAIT_SEC)
    try:
        if is_us:
            return trades, *api.get_us_balance()
        else:
            return trades, *api.get_kr_balance()
    except Exception as e:
        # 주문은 이미 나갔으므로 여기서 실패해도 사이클을 중단하지 않는다
        logger.warning(f"매매 후 잔고 재조회 실패 ({ticker}), 매매 전 잔고 사용: {e}")
        return trades, holdings, cash


def _stop_after_orders(market: str, attempted: list, exc: Exception):
    """
    사이클 도중 오류 처리
    - 아직 주문을 보내지 않았으면 예외를 다시 올려 main이 재시도하게 한다
    - 이미 주문을 보냈으면 재시도 시 같은 주문이 중복되므로 여기서 사이클을 끝낸다
    """
    if not attempted:
        raise exc
    logger.error(f"{market} 주문 전송 후 오류 - 재시도하지 않음: {exc}", exc_info=exc)
    notify_error(
        f"{market} 매매 도중 오류 (주문 전송: {', '.join(attempted)})\n"
        f"{exc}\n"
        f"※ 중복 주문 방지를 위해 오늘 {market} 매매는 재시도하지 않습니다. 잔고를 확인하세요."
    )


# ════════════════════════════════════════════
# 월 증액
# ════════════════════════════════════════════

def handle_monthly_increase(config: dict, all_tickers: dict) -> dict:
    """
    모든 종목 월 증액
    Returns: {ticker: (old_base, new_base)}
    """
    this_month = date.today().strftime("%Y-%m")
    last_month = get_last_increased_month(config)
    
    if this_month == last_month:
        return {}
    
    if not is_first_trading_day_of_month():
        return {}
    
    increases = {}
    for ticker in all_tickers.keys():
        rate = get_monthly_rate(config, ticker)
        if rate > 0:
            old_base = get_base_value(config, ticker)
            new_base = old_base * (1 + rate)
            update_base(ticker, new_base)
            increases[ticker] = (old_base, new_base)
            logger.info(f"월 증액: {ticker} ${old_base:.2f} → ${new_base:.2f}")
    
    if increases:
        update_monthly_increase_date()
        notify_monthly_increase(increases)
    
    return increases


# ════════════════════════════════════════════
# 미국장 전략
# ════════════════════════════════════════════

def run_us_strategy(api):
    """미국장 GBV 전략 실행"""
    logger.info("━━━ 미국장 매매 시작 ━━━")
    config = load_config()
    
    # 설정 로드
    all_us_tickers = get_all_us_tickers(config)
    outside_tqqq = get_outside_tqqq(config)
    
    # 월 증액
    handle_monthly_increase(config, all_us_tickers)
    config = load_config()  # 증액 후 재로드
    
    # 현재가 조회
    prices = {}
    for ticker in all_us_tickers.keys():
        try:
            prices[ticker] = api.get_us_price(ticker)
            logger.info(f"{ticker} 현재가: ${prices[ticker]:,.2f}")
        except Exception as e:
            logger.error(f"현재가 조회 실패 ({ticker}): {e}")
            return
    
    # 잔고 조회
    holdings, cash_usd = api.get_us_balance()
    logger.info(f"달러 현금: ${cash_usd:,.2f} | 잔고: {holdings}")
    
    all_trades = []
    attempted = []   # 주문을 전송한 종목 (오류 시 재시도 여부 판단)
    
    try:
        # ═══ GBV 실행 ═══
        for ticker in all_us_tickers.keys():
            base = get_base_value(config, ticker)
            trades, holdings, cash_usd = execute_gbv(
                api, ticker, prices[ticker], base,
                outside_tqqq, is_us=True, attempted=attempted
            )
            all_trades.extend(trades)
    
        # 최종 잔고 및 총 자산 계산 (마지막 종목 체결 반영 대기)
        if all_trades:
            time.sleep(SETTLEMENT_WAIT_SEC)
        holdings, cash_usd = api.get_us_balance()
        total_assets = cash_usd
        for ticker, info in holdings.items():
            total_assets += info["qty"] * prices.get(ticker, 0)
        total_assets += outside_tqqq * prices.get("TQQQ", 0)
    
        # 원화 환산
        usd_krw_rate = api.get_usd_krw_rate()
        total_krw = total_assets * usd_krw_rate
    
        # 리포트 저장
        tqqq_base = get_base_value(config, "TQQQ")
        save_report(
            market="미국장",
            trades=all_trades,
            holdings=holdings,
            cash=cash_usd,
            total_assets=total_assets,
            tqqq_base=tqqq_base,
            prices=prices,
            currency="USD",
            total_assets_krw=total_krw,
            usd_krw_rate=usd_krw_rate
        )
    
        # 완료 알림
        notify_cycle_complete("미국장", all_trades, holdings, cash_usd, 
                             total_assets, prices, outside_tqqq)
    
        logger.info("━━━ 미국장 매매 완료 ━━━")
    except Exception as e:
        _stop_after_orders("미국장", attempted, e)


# ════════════════════════════════════════════
# 국내장 전략
# ════════════════════════════════════════════

def run_kr_strategy(api):
    """국내장 GBV 전략 실행"""
    logger.info("━━━ 국내장 매매 시작 ━━━")
    config = load_config()
    
    # 설정 로드
    all_kr_tickers = get_all_kr_tickers(config)
    
    if not all_kr_tickers:
        logger.info("국내 종목 없음 - 매매 중단")
        return
    
    # 월 증액
    handle_monthly_increase(config, all_kr_tickers)
    config = load_config()
    
    # 현재가 조회
    prices = {}
    for ticker in all_kr_tickers.keys():
        try:
            prices[ticker] = api.get_kr_price(ticker)
            # 잔고 키(6자리)로도 조회되도록 (평가금/리포트 계산용)
            prices.setdefault(normalize_kr_ticker(ticker), prices[ticker])
            logger.info(f"{ticker} 현재가: ₩{int(prices[ticker]):,}")
        except Exception as e:
            logger.error(f"현재가 조회 실패 ({ticker}): {e}")
            return
    
    # 잔고 조회
    holdings, cash_krw = api.get_kr_balance()
    logger.info(f"원화 현금: ₩{int(cash_krw):,} | 잔고: {holdings}")
    
    all_trades = []
    attempted = []   # 주문을 전송한 종목 (오류 시 재시도 여부 판단)
    
    try:
        # ═══ GBV 실행 ═══
        for ticker in all_kr_tickers.keys():
            base = get_base_value(config, ticker)
            trades, holdings, cash_krw = execute_gbv(
                api, ticker, prices[ticker], base,
                0, is_us=False, attempted=attempted
            )
            all_trades.extend(trades)
    
        # 최종 잔고 및 총 자산 (마지막 종목 체결 반영 대기)
        if all_trades:
            time.sleep(SETTLEMENT_WAIT_SEC)
        holdings, cash_krw = api.get_kr_balance()
        total_assets = cash_krw
        for ticker, info in holdings.items():
            total_assets += info["qty"] * prices.get(ticker, 0)
    
        # 리포트 저장
        save_report(
            market="국내장",
            trades=all_trades,
            holdings=holdings,
            cash=cash_krw,
            total_assets=total_assets,
            tqqq_base=0,
            prices=prices,
            currency="KRW"
        )
    
        # 완료 알림
        notify_cycle_complete("국내장", all_trades, holdings, cash_krw,
                             total_assets, prices, 0)
    
        logger.info("━━━ 국내장 매매 완료 ━━━")
    except Exception as e:
        _stop_after_orders("국내장", attempted, e)
